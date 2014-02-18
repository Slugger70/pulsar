import threading
try:
    from Queue import Queue
except ImportError:
    from queue import Queue
from os import getenv

from .client import JobClient
from .client import InputCachingJobClient
from .client import MessageJobClient
from .interface import HttpLwrInterface
from .interface import LocalLwrInterface
from .object_client import ObjectStoreClient
from .transport import get_transport
from .util import TransferEventManager
from .destination import url_to_destination_params
from .amqp_exchange import LwrExchange


from logging import getLogger
log = getLogger(__name__)

DEFAULT_TRANSFER_THREADS = 2


def build_client_manager(**kwargs):
    if 'job_manager' in kwargs:
        return ClientManager(**kwargs)  # TODO: Consider more separation here.
    elif 'url' in kwargs:
        return MessageQueueClientManager(**kwargs)
    else:
        return ClientManager(**kwargs)


class ClientManager(object):
    """
    Factory to create LWR clients, used to manage potential shared
    state between multiple client connections.
    """
    def __init__(self, **kwds):
        if 'job_manager' in kwds:
            self.job_manager_interface_class = LocalLwrInterface
            self.job_manager_interface_args = dict(job_manager=kwds['job_manager'], file_cache=kwds['file_cache'])
        else:
            self.job_manager_interface_class = HttpLwrInterface
            transport_type = kwds.get('transport_type', None)
            transport = get_transport(transport_type)
            self.job_manager_interface_args = dict(transport=transport)
        cache = kwds.get('cache', None)
        if cache is None:
            cache = _environ_default_int('LWR_CACHE_TRANSFERS')
        if cache:
            log.info("Setting LWR client class to caching variant.")
            self.client_cacher = ClientCacher(**kwds)
            self.client_class = InputCachingJobClient
            self.extra_client_kwds = {"client_cacher": self.client_cacher}
        else:
            log.info("Setting LWR client class to standard, non-caching variant.")
            self.client_class = JobClient
            self.extra_client_kwds = {}

    def get_client(self, destination_params, job_id):
        destination_params = _parse_destination_params(destination_params)
        job_manager_interface_class = self.job_manager_interface_class
        job_manager_interface_args = dict(destination_params=destination_params, **self.job_manager_interface_args)
        job_manager_interface = job_manager_interface_class(**job_manager_interface_args)
        return self.client_class(destination_params, job_id, job_manager_interface, **self.extra_client_kwds)

    def shutdown(self):
        pass


class MessageQueueClientManager(object):

    def __init__(self, **kwds):
        self.url = kwds.get('url')
        self.manager_name = kwds.get("manager", "_default_")
        self.exchange = LwrExchange(self.url, self.manager_name)
        self.final_status_cache = {}
        self.callback_lock = threading.Lock()
        self.callback_thread = None
        self.active = True

    def ensure_has_job_completes_callback(self, callback):
        with self.callback_lock:
            if self.callback_thread is not None:
                return

            def callback_wrapper(body, message):
                try:
                    if "job_id" in body:
                        self.final_status_cache[body["job_id"]] = body
                    callback(body)
                except Exception:
                    log.exception("Failure processing job status update message.")
                message.ack()

            def run():
                self.exchange.consume("complete", callback_wrapper, check=self)

            thread = threading.Thread(
                name="lwr_client_%s_complete_callback" % self.manager_name,
                target=run
            )
            thread.daemon = False  # Lets not interrupt processing of this.
            thread.start()
            self.callback_thread = thread

    def shutdown(self):
        self.active = False

    def __nonzero__(self):
        return self.active

    def get_client(self, destination_params, job_id):
        destination_params = _parse_destination_params(destination_params)
        return MessageJobClient(destination_params, job_id, self)


class ObjectStoreClientManager(object):

    def __init__(self, **kwds):
        if 'object_store' in kwds:
            self.interface_class = LocalLwrInterface
            self.interface_args = dict(object_store=kwds['object_store'])
        else:
            self.interface_class = HttpLwrInterface
            transport_type = kwds.get('transport_type', None)
            transport = get_transport(transport_type)
            self.interface_args = dict(transport=transport)
        self.extra_client_kwds = {}

    def get_client(self, client_params):
        interface_class = self.interface_class
        interface_args = dict(destination_params=client_params, **self.interface_args)
        interface = interface_class(**interface_args)
        return ObjectStoreClient(interface)


class ClientCacher(object):

    def __init__(self, **kwds):
        self.event_manager = TransferEventManager()
        default_transfer_threads = _environ_default_int('LWR_CACHE_THREADS', DEFAULT_TRANSFER_THREADS)
        num_transfer_threads = int(kwds.get('transfer_threads', default_transfer_threads))
        self.__init_transfer_threads(num_transfer_threads)

    def queue_transfer(self, client, path):
        self.transfer_queue.put((client, path))

    def acquire_event(self, input_path):
        return self.event_manager.acquire_event(input_path)

    def _transfer_worker(self):
        while True:
            transfer_info = self.transfer_queue.get()
            try:
                self.__perform_transfer(transfer_info)
            except BaseException as e:
                log.warn("Transfer failed.")
                log.exception(e)
                pass
            self.transfer_queue.task_done()

    def __perform_transfer(self, transfer_info):
        (client, path) = transfer_info
        event_holder = self.event_manager.acquire_event(path, force_clear=True)
        failed = True
        try:
            client.cache_insert(path)
            failed = False
        finally:
            event_holder.failed = failed
            event_holder.release()

    def __init_transfer_threads(self, num_transfer_threads):
        self.num_transfer_threads = num_transfer_threads
        self.transfer_queue = Queue()
        for i in range(num_transfer_threads):
            t = threading.Thread(target=self._transfer_worker)
            t.daemon = True
            t.start()


def _parse_destination_params(destination_params):
    try:
        unicode_type = unicode
    except NameError:
        unicode_type = str
    if isinstance(destination_params, str) or isinstance(destination_params, unicode_type):
        destination_params = url_to_destination_params(destination_params)
    return destination_params


def _environ_default_int(variable, default="0"):
    val = getenv(variable, default)
    int_val = int(default)
    if str(val).isdigit():
        int_val = int(val)
    return int_val

__all__ = [ClientManager, ObjectStoreClientManager, HttpLwrInterface]
