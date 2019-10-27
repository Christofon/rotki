import json
import logging
import traceback
from http import HTTPStatus
from typing import Any, Dict, List, Optional, Tuple, cast

import gevent
from flask import Response, make_response
from gevent.event import Event
from gevent.lock import Semaphore

from rotkehlchen.api.v1.encoding import TradeSchema
from rotkehlchen.assets.asset import Asset
from rotkehlchen.errors import (
    AuthenticationError,
    IncorrectApiKeyFormat,
    RotkehlchenPermissionError,
)
from rotkehlchen.exchanges.data_structures import Trade
from rotkehlchen.inquirer import Inquirer
from rotkehlchen.logging import RotkehlchenLogsAdapter
from rotkehlchen.serializer import process_result
from rotkehlchen.typing import (
    AssetAmount,
    Fee,
    FiatAsset,
    Location,
    Price,
    Timestamp,
    TradePair,
    TradeType,
)

OK_RESULT = {'result': True, 'message': ''}

ERROR_STATUS_CODES = [
    HTTPStatus.CONFLICT,
    HTTPStatus.REQUEST_TIMEOUT,
    HTTPStatus.PAYMENT_REQUIRED,
    HTTPStatus.BAD_REQUEST,
    HTTPStatus.NOT_FOUND,
    HTTPStatus.UNAUTHORIZED,
]


def _wrap_in_ok_result(result: Any) -> Dict[str, Any]:
    return {'result': result, 'message': ''}


def _wrap_in_result(result: Any, message: str) -> Dict[str, Any]:
    return {'result': result, 'message': message}


def wrap_in_fail_result(message: str) -> Dict[str, Any]:
    return {'result': None, 'message': message}


def api_response(
        result: Dict[str, Any],
        status_code: HTTPStatus = HTTPStatus.OK,
) -> Response:
    if status_code == HTTPStatus.NO_CONTENT:
        assert not result, "Provided 204 response with non-zero length response"
        data = ""
    else:
        data = json.dumps(result)

    log.debug("Request successful", response=result, status_code=status_code)
    response = make_response(
        (data, status_code, {"mimetype": "application/json", "Content-Type": "application/json"}),
    )
    return response


logger = logging.getLogger(__name__)
log = RotkehlchenLogsAdapter(logger)


class RestAPI():
    """ The Object holding the logic that runs inside all the API calls"""
    def __init__(self, rotkehlchen):
        self.rotkehlchen = rotkehlchen
        self.stop_event = Event()
        mainloop_greenlet = self.rotkehlchen.start()
        mainloop_greenlet.link_exception(self._handle_killed_greenlets)
        # Greenlets that will be waited for when we shutdown
        self.waited_greenlets = [mainloop_greenlet]
        # Greenlets that can be killed instead of waited for when we shutdown
        self.killable_greenlets = []
        self.task_lock = Semaphore()
        self.task_id = 0
        self.task_results = {}

        self.trade_schema = TradeSchema()

    # - Private functions not exposed to the API
    def _new_task_id(self):
        with self.task_lock:
            task_id = self.task_id
            self.task_id += 1
        return task_id

    def _write_task_result(self, task_id, result):
        with self.task_lock:
            self.task_results[task_id] = result

    def _handle_killed_greenlets(self, greenlet):
        if not greenlet.exception:
            log.warning('handle_killed_greenlets without an exception')
            return

        log.error(
            'Greenlet for task {} dies with exception: {}.\n'
            'Exception Name: {}\nException Info: {}\nTraceback:\n {}'
            .format(
                greenlet.task_id,
                greenlet.exception,
                greenlet.exc_info[0],
                greenlet.exc_info[1],
                ''.join(traceback.format_tb(greenlet.exc_info[2])),
            ))
        # also write an error for the task result
        result = {
            'error': str(greenlet.exception),
        }
        self._write_task_result(greenlet.task_id, result)

    def _do_query_async(self, command: str, task_id: int, **kwargs) -> None:
        result = getattr(self, command)(**kwargs)
        self._write_task_result(task_id, result)

    def _query_async(self, command: str, **kwargs) -> Response:
        task_id = self._new_task_id()
        log.debug("NEW TASK {} (kwargs:{}) with ID: {}".format(command, kwargs, task_id))
        greenlet = gevent.spawn(
            self._do_query_async,
            command,
            task_id,
            **kwargs,
        )
        greenlet.task_id = task_id
        greenlet.link_exception(self._handle_killed_greenlets)
        self.killable_greenlets.append(greenlet)
        return api_response(_wrap_in_ok_result({'task_id': task_id}), status_code=HTTPStatus.OK)

    # - Public functions not exposed via the rest api
    def stop(self):
        self.rotkehlchen.shutdown()
        log.debug('Waiting for greenlets')
        gevent.wait(self.waited_greenlets)
        log.debug('Waited for greenlets. Killing all other greenlets')
        gevent.killall(self.killable_greenlets)
        log.debug('Greenlets killed. Killing zerorpc greenlet')
        log.debug('Shutdown completed')
        logging.shutdown()
        self.stop_event.set()

    # - Public functions exposed via the rest api

    def logout(self) -> Response:
        # Kill all queries apart from the main loop -- perhaps a bit heavy handed
        # but the other options would be:
        # 1. to wait for all of them. That could take a lot of time, for no reason.
        #    All results would be discarded anyway since we are logging out.
        # 2. Have an intricate stop() notification system for each greenlet, but
        #   that is going to get complicated fast.
        gevent.killall(self.killable_greenlets)
        with self.task_lock:
            self.task_results = {}
        self.rotkehlchen.logout()
        return api_response(result=OK_RESULT, status_code=HTTPStatus.OK)

    def set_settings(self, settings: Dict[str, Any]) -> Response:
        _, message = self.rotkehlchen.set_settings(settings)
        new_settings = process_result(self.rotkehlchen.data.db.get_settings())
        result_dict = {'result': new_settings, 'message': message}
        status_code = HTTPStatus.OK if message == '' else HTTPStatus.CONFLICT
        return api_response(result=result_dict, status_code=status_code)

    def get_settings(self) -> Response:
        result_dict = _wrap_in_ok_result(process_result(self.rotkehlchen.data.db.get_settings()))
        return api_response(result=result_dict, status_code=HTTPStatus.OK)

    def query_task_outcome(self, task_id: int) -> Response:
        with self.task_lock:
            for greenlet in self.killable_greenlets:
                if greenlet.task_id == task_id:
                    # The task is pending
                    result_dict = {
                        'result': {'status': 'pending', 'outcome': None},
                        'message': f'The task with id {task_id} is still pending',
                    }
                    return api_response(result=result_dict, status_code=HTTPStatus.OK)
            ret = self.task_results.pop(int(task_id), None)

        if ret is None:
            # The task has not been found
            result_dict = {
                'result': {'status': 'not-found', 'outcome': None},
                'message': f'No task with id {task_id} found',
            }
            return api_response(result=result_dict, status_code=HTTPStatus.NOT_FOUND)
        # Task has completed and we just got the outcome
        result_dict = {
            'result': {'status': 'completed', 'outcome': process_result(ret)},
            'message': '',
        }
        return api_response(result=result_dict, status_code=HTTPStatus.OK)

    @staticmethod
    def get_fiat_exchange_rates(currencies: List[str]) -> Response:
        fiat_currencies = cast(List[FiatAsset], currencies)
        rates = Inquirer().get_fiat_usd_exchange_rates(fiat_currencies)
        res = process_result(rates)
        return api_response(_wrap_in_ok_result(res), status_code=HTTPStatus.OK)

    def setup_exchange(self, name: str, api_key: str, api_secret: str) -> Response:
        result, message = self.rotkehlchen.setup_exchange(name, api_key, api_secret)
        status_code = HTTPStatus.OK
        if result is None:
            status_code = HTTPStatus.CONFLICT
        return api_response(_wrap_in_result(result, message), status_code=status_code)

    def remove_exchange(self, name: str) -> Response:
        result, message = self.rotkehlchen.remove_exchange(name)
        status_code = HTTPStatus.OK
        if result is None:
            status_code = HTTPStatus.CONFLICT
        return api_response(_wrap_in_result(result, message), status_code=status_code)

    def _query_exchange_balances(self, name: str) -> Tuple[Optional[dict], str]:
        exchange_obj = self.rotkehlchen.exchange_manager.connected_exchanges.get(name, None)
        if not exchange_obj:
            return None, f'Could not query balances for {name} since it is not registered'

        return exchange_obj.query_balances()

    def query_exchange_balances(self, name: str, async_query: bool) -> Response:
        if async_query:
            return self._query_async(command='_query_exchange_balances', name=name)

        balances, msg = self._query_exchange_balances(name=name)
        if balances is None:
            return api_response(wrap_in_fail_result(msg), status_code=HTTPStatus.CONFLICT)

        return api_response(_wrap_in_ok_result(process_result(balances)), HTTPStatus.OK)

    def _query_blockchain_balances(self, name: str) -> Tuple[Optional[Dict[str, Dict]], str]:
        return self.rotkehlchen.blockchain.query_balances(name=name)

    def query_blockchain_balances(self, name: str, async_query: bool) -> Response:
        if async_query:
            return self._query_async(command='_query_blockchain_balances', name=name)

        balances, msg = self._query_blockchain_balances(name=name)
        if balances is None:
            return api_response(wrap_in_fail_result(msg), status_code=HTTPStatus.CONFLICT)

        return api_response(_wrap_in_ok_result(process_result(balances)), HTTPStatus.OK)

    def query_fiat_balances(self) -> Response:
        balances = self.rotkehlchen.query_fiat_balances()
        return api_response(_wrap_in_ok_result(process_result(balances)), HTTPStatus.OK)

    def get_trades(
            self,
            from_ts: Optional[Timestamp],
            to_ts: Optional[Timestamp],
            location: Optional[Location],
    ) -> Response:
        trades = self.rotkehlchen.data.db.get_trades(
            from_ts=from_ts,
            to_ts=to_ts,
            location=location,
        )
        result = [
            self.trade_schema.dump(trade) for trade in trades
        ]
        return api_response(_wrap_in_ok_result(result), status_code=HTTPStatus.OK)

    def add_trade(
            self,
            timestamp: Timestamp,
            location: Location,
            pair: TradePair,
            trade_type: TradeType,
            amount: AssetAmount,
            rate: Price,
            fee: Fee,
            fee_currency: Asset,
            link: str,
            notes: str,
    ) -> Response:
        trade = Trade(
            timestamp=timestamp,
            location=location,
            pair=pair,
            trade_type=trade_type,
            amount=amount,
            rate=rate,
            fee=fee,
            fee_currency=fee_currency,
            link=link,
            notes=notes,
        )
        self.rotkehlchen.data.db.add_trades([trade])
        # For the outside world we should also add the trade identifier
        result_dict = self.trade_schema.dump(trade)
        result_dict['trade_id'] = trade.identifier
        result_dict = _wrap_in_ok_result(result_dict)
        return api_response(result_dict, status_code=HTTPStatus.OK)

    def edit_trade(
            self,
            trade_id: str,
            timestamp: Timestamp,
            pair: TradePair,
            trade_type: TradeType,
            amount: AssetAmount,
            rate: Price,
            fee: Fee,
            fee_currency: Asset,
            link: str,
            notes: str,
    ) -> Response:
        trade = Trade(
            timestamp=timestamp,
            location=Location.EXTERNAL,
            pair=pair,
            trade_type=trade_type,
            amount=amount,
            rate=rate,
            fee=fee,
            fee_currency=fee_currency,
            link=link,
            notes=notes,
        )
        result, msg = self.rotkehlchen.data.db.edit_trade(old_trade_id=trade_id, trade=trade)
        if not result:
            return api_response(wrap_in_fail_result(msg), status_code=HTTPStatus.CONFLICT)

        # For the outside world we should also add the trade identifier
        result_dict = self.trade_schema.dump(trade)
        result_dict['trade_id'] = trade.identifier
        result_dict = _wrap_in_ok_result(result_dict)
        return api_response(result_dict, status_code=HTTPStatus.OK)

    def delete_trade(self, trade_id: str) -> Response:
        result, msg = self.rotkehlchen.data.db.delete_trade(trade_id)
        if not result:
            return api_response(wrap_in_fail_result(msg), status_code=HTTPStatus.CONFLICT)

        return api_response(_wrap_in_ok_result(True), status_code=HTTPStatus.OK)

    def get_users(self) -> Response:
        result = self.rotkehlchen.data.get_users()
        result_dict = _wrap_in_ok_result(result)
        return api_response(result_dict, status_code=HTTPStatus.OK)

    def create_new_user(
            self,
            name: str,
            password: str,
            sync_approval: str,
            premium_api_key: str,
            premium_api_secret: str,
    ) -> Response:

        result_dict: Dict[str, Any] = {'result': None, 'message': ''}
        if (
                premium_api_key != '' and premium_api_secret == '' or
                premium_api_secret != '' and premium_api_key == ''
        ):
            result_dict['message'] = 'Must provide both or neither of api key/secret'
            return api_response(result_dict, status_code=HTTPStatus.BAD_REQUEST)

        try:
            result_dict = self.rotkehlchen.unlock_user(
                user=name,
                password=password,
                create_new=True,
                sync_approval=sync_approval,
                api_key=premium_api_key,
                api_secret=premium_api_secret,
            )
        except AuthenticationError as e:
            result_dict['message'] = str(e)
            return api_response(result_dict, status_code=HTTPStatus.CONFLICT)
        except RotkehlchenPermissionError as e:
            result_dict['message'] = str(e)
            return api_response(result_dict, status_code=HTTPStatus.MULTIPLE_CHOICES)
        # Success!
        result_dict['result'] = {
            'exchanges': self.rotkehlchen.exchange_manager.get_connected_exchange_names(),
            'premium': self.rotkehlchen.premium is not None,
            'settings': process_result(self.rotkehlchen.data.db.get_settings),
        }
        return api_response(result_dict, status_code=HTTPStatus.OK)

    def user_login(
            self,
            name: str,
            password: str,
            sync_approval: str,
    ) -> Response:

        result_dict: Dict[str, Any] = {'result': None, 'message': ''}
        try:
            result_dict = self.rotkehlchen.unlock_user(
                user=name,
                password=password,
                create_new=False,
                sync_approval=sync_approval,
                api_key='',
                api_secret='',
            )
        except AuthenticationError as e:
            result_dict['message'] = str(e)
            return api_response(result_dict, status_code=HTTPStatus.CONFLICT)
        except RotkehlchenPermissionError as e:
            result_dict['message'] = str(e)
            return api_response(result_dict, status_code=HTTPStatus.MULTIPLE_CHOICES)
        # Success!
        result_dict['result'] = {
            'exchanges': self.rotkehlchen.exchange_manager.get_connected_exchange_names(),
            'premium': self.rotkehlchen.premium is not None,
            'settings': process_result(self.rotkehlchen.data.db.get_settings),
        }
        return api_response(result_dict, status_code=HTTPStatus.OK)

    def user_logout(self, name: str) -> Response:
        result_dict: Dict[str, Any] = {'result': None, 'message': ''}
        if not self.rotkehlchen.user_is_logged_in:
            result_dict['message'] = 'No user is currently logged in'
            return api_response(result_dict, status_code=HTTPStatus.CONFLICT)

        if name != self.rotkehlchen.data.username:
            result_dict['message'] = f'Provided user {name} is not the logged in user'
            return api_response(result_dict, status_code=HTTPStatus.CONFLICT)

        result_dict['result'] = True
        return api_response(result_dict, status_code=HTTPStatus.OK)

    def user_set_premium_credentials(
            self,
            name: str,
            api_key: str,
            api_secret: str,
    ) -> Response:
        result_dict: Dict[str, Any] = {'result': None, 'message': ''}
        if not self.rotkehlchen.user_is_logged_in:
            result_dict['message'] = 'No user is currently logged in'
            return api_response(result_dict, status_code=HTTPStatus.CONFLICT)

        if name != self.rotkehlchen.data.username:
            result_dict['message'] = f'Provided user {name} is not the logged in user'
            return api_response(result_dict, status_code=HTTPStatus.CONFLICT)

        try:
            self.rotkehlchen.set_premium_credentials(api_key, api_secret)
        except (AuthenticationError, IncorrectApiKeyFormat) as e:
            result_dict['message'] = str(e)
            return api_response(result_dict, status_code=HTTPStatus.UNAUTHORIZED)

        result_dict['result'] = True
        return api_response(result_dict, status_code=HTTPStatus.OK)

    def query_owned_assets(self) -> Response:
        result = process_result(self.rotkehlchen.data.db.query_owned_assets())
        return api_response(_wrap_in_ok_result(result), status_code=HTTPStatus.OK)

    def query_netvalue_data(self) -> Response:
        data = self.rotkehlchen.data.db.get_netvalue_data()
        result = process_result({'times': data[0], 'data': data[1]})
        return api_response(_wrap_in_ok_result(result), status_code=HTTPStatus.OK)

    def query_timed_balances_data(
            self,
            asset: Asset,
            from_timestamp: Optional[Timestamp],
            to_timestamp: Optional[Timestamp],
    ) -> Response:
        data = self.rotkehlchen.data.db.query_timed_balances(
            from_ts=from_timestamp,
            to_ts=to_timestamp,
            asset=asset,
        )
        result = process_result(data)
        return api_response(_wrap_in_ok_result(result), status_code=HTTPStatus.OK)

    def query_value_distribution_data(self, distribution_by: str) -> Response:
        if distribution_by == 'location':
            data = self.rotkehlchen.data.db.get_latest_location_value_distribution()

        else:
            data = self.rotkehlchen.data.db.get_latest_asset_value_distribution()

        result = process_result(data)
        return api_response(_wrap_in_ok_result(result), status_code=HTTPStatus.OK)