# Copyright (C) 2015. BMW Car IT GmbH. All rights reserved.
"""Handlers are classes that assist dlt_broker in receiving and
filtering DLT messages
"""
from collections import defaultdict
import ctypes
import logging
from multiprocessing import Lock, Process, Value
from queue import Empty
import socket
import time
from threading import Thread, Event

from dlt.dlt import DLTClient, DLT_DAEMON_TCP_PORT, py_dlt_client_main_loop


DLT_CLIENT_TIMEOUT = 5
logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class DLTTimeValue(object):
    """Create a share memory to pass the timestamp between processes

    The type of dlt time is float (4 bytes). There are several ways to send
    the value between DLTMessageHandler (it's a process) and DLTBroker. Since
    DLTMessageHandler has to send the value many times every second, choosing a
    lightweight solution is must.

    khiz678 studied and evaluated the following solutions for the problem.
      - multiprocessing.Queue (Queue in the following discussion)
      - multiprocessing.Pipe (Pipe in the following discussion)
      - multiprocessing.Value (Value in the following discussion)

    Value is our final solution. Queue's implementation is based on Pipe (in
    cpython). If the solution is based on Queue or Pipe, it needs another
    thread in DLTBroker process to receive and write the value to a local
    variable. The solution based on Value does not have such problem, only
    assigns the value to the shared memory directly.

    khiz678 also did a simple benchmark for the Value solution. It could
    receive more than 100000 timestamps per second.  It's twice faster than
    Pipe's implementation.
    """

    def __init__(self, default_value=0.0):
        self._timestamp_mem = Value(ctypes.c_double, default_value)

    @property
    def timestamp(self):
        """Get the seconds from 1970/1/1 0:00:00

        :rtype: float
        """
        with self._timestamp_mem.get_lock():
            return self._timestamp_mem.value

    @timestamp.setter
    def timestamp(self, new_timestamp):
        with self._timestamp_mem.get_lock():
            self._timestamp_mem.value = new_timestamp


class DLTFilterAckMessageHandler(Thread):
    """Receive filter-set ack message and pass it to the corresponding ack queue"""

    def __init__(self, filter_ack_queue):  # (multiprocessing.Queue[Tuple[ContextQueueID, bool]]) -> None
        super(DLTFilterAckMessageHandler, self).__init__()

        self.filter_ack_queue = filter_ack_queue

        self.stop_flag = Event()

        self.context_map_lock = Lock()
        self.context_map = {}  # Dict[ContextQueueID, Queue.Queue[bool]]

    def stop(self):
        """Stops thread execution"""
        self.stop_flag.set()
        self.filter_ack_queue.put((None, None))
        if self.is_alive():
            self.join()

    def register(self, filter_ack_queue):  # (Queue.Queue[bool]) -> None
        """Register an ack queue"""
        with self.context_map_lock:
            self.context_map[id(filter_ack_queue)] = filter_ack_queue

    def unregister(self, filter_ack_queue):  # (Queue.Queue[bool]) -> None
        """Unregister an ack queue"""
        with self.context_map_lock:
            key = id(filter_ack_queue)

            if key in self.context_map:
                del self.context_map[key]

    def run(self):
        """Run the thread to recieve the message and dispatch it"""
        while not self.stop_flag.is_set():
            queue_ack_id, enable = self.filter_ack_queue.get()

            if not queue_ack_id:
                continue

            with self.context_map_lock:
                if queue_ack_id in self.context_map:
                    self.context_map[queue_ack_id].put(enable)
                else:
                    logger.warning("Could not send an ack to the queue: %s", queue_ack_id)


class DLTContextHandler(Thread):
    """Communication layer between the DLTContext instances and
    DLTMessageHandler Process.

    This class handles the transfer of messages between the process
    receiving traces from the DLT Daemon and the DLTContext queues.
    """

    def __init__(self, filter_queue, message_queue):
        super(DLTContextHandler, self).__init__()
        self.stop_flag = Event()
        self.context_map = {}
        self.lock = Lock()
        self.filter_queue = filter_queue
        self.message_queue = message_queue

    def _make_send_filter_msg(self, queue, filters, is_register, context_filter_ack_queue=None):
        """Send a filter message to the filter message queue"""
        queue_id = id(queue)

        if context_filter_ack_queue:
            return queue_id, id(context_filter_ack_queue), filters, is_register

        return queue_id, filters, is_register

    def register(self, queue, filters=None, context_filter_ack_queue=None):
        """Register a queue to collect messages matching specific filters

        :param Queue queue: The new queue to add
        :param tuple filters: An tuple with (apid, ctid) used to filter
                              messages that go into this queue.
        """
        if filters is None:
            filters = [(None, None)]

        queue_id = id(queue)  # - unique identifier for this queue
        with self.lock:
            self.context_map[queue_id] = (queue, filters)

        # - inform the DLTMessageHandler process about this new
        # (queue, filter) pair
        self.filter_queue.put(
            self._make_send_filter_msg(queue, filters, True, context_filter_ack_queue=context_filter_ack_queue)
        )

    def unregister(self, queue, context_filter_ack_queue=None):
        """Remove a queue from set of queues being handled

        :param Queue queue: The queue to remove
        """
        queue_id = id(queue)
        _, filters = self.context_map.get(queue_id, (None, None))
        if filters:
            with self.lock:
                try:
                    del self.context_map[queue_id]
                except KeyError:
                    pass

            # - inform the DLTMessageHandler process about removal of this
            # (queue, filter) pair
            self.filter_queue.put(
                self._make_send_filter_msg(queue, filters, False, context_filter_ack_queue=context_filter_ack_queue)
            )

    def run(self):
        """The thread's main loop"""
        while not self.stop_flag.is_set():
            queue_id, message = None, None
            try:
                if self.message_queue.full():
                    logger.error("message_queue is full ! put() on this queue will block")
                queue_id, message = self.message_queue.get_nowait()
            except Empty:
                pass

            if message:
                queue, _ = self.context_map.get(queue_id, (None, None))
                if queue:
                    queue.put(message)
            else:
                time.sleep(0.01)

    def stop(self):
        """Stops thread execution"""
        self.stop_flag.set()
        self.filter_queue.close()
        if self.is_alive():
            self.join()


class DLTMessageHandler(Process):
    """Process receiving the DLT messages and handing them to DLTContextHandler

    This process instance is responsible for collecting messages from
    the DLT daemon, tagging them with the correct queue id and placing
    them on the messages queue.
    """

    def __init__(
        self, filter_queue, message_queue, mp_stop_event, client_cfg, dlt_time_value=None, filter_ack_queue=None
    ):
        self.filter_queue = filter_queue
        self.filter_ack_queue = filter_ack_queue
        self.message_queue = message_queue
        self.mp_stop_flag = mp_stop_event
        super(DLTMessageHandler, self).__init__()

        # - dict mapping filters to queue ids
        self.context_map = defaultdict(list)

        self._ip_address = client_cfg["ip_address"]
        self._port = client_cfg.get("port", DLT_DAEMON_TCP_PORT)
        self._filename = client_cfg.get("filename")
        self.verbose = client_cfg.get("verbose", 0)
        self.timeout = client_cfg.get("timeout", DLT_CLIENT_TIMEOUT)
        self._client = None
        self.tracefile = None

        self._dlt_time_value = dlt_time_value

    def _client_connect(self):
        """Create a new DLTClient

        :param int timeout: Time in seconds to wait for connection.
        :returns: True if connected, False otherwise
        :rtype: bool
        """
        logger.debug(
            "Creating DLTClient (ip_address='%s', Port='%s', logfile='%s')",
            self._ip_address,
            self._port,
            self._filename,
        )
        self._client = DLTClient(servIP=self._ip_address, port=self._port, verbose=self.verbose)
        connected = self._client.connect(self.timeout)
        if connected:
            logger.info("DLTClient connected to %s", self._client.servIP)
        return connected

    def _process_filter_queue(self):
        """Check if filters have been added or need to be removed"""
        while not self.filter_queue.empty():
            queue_ack_id = None

            msg = self.filter_queue.get_nowait()
            logger.debug("Process filter queue message: %s", msg)
            if isinstance(msg, tuple) and len(msg) == 4:
                queue_id, queue_ack_id, filters, add = msg
            else:
                queue_id, filters, add = msg

            if add:
                for apid_ctid in filters:
                    self.context_map[apid_ctid].append(queue_id)
            else:
                try:
                    for apid_ctid in filters:
                        self.context_map[apid_ctid].remove(queue_id)
                        if not self.context_map[apid_ctid]:
                            del self.context_map[apid_ctid]
                except (KeyError, ValueError):
                    # - queue_id already removed or not inserted
                    pass

            if self.filter_ack_queue and queue_ack_id:
                logger.debug("Send filter ack message: queue_ack_id: %s, add: %s", queue_ack_id, add)
                self.filter_ack_queue.put((queue_ack_id, add))

    def handle(self, message):
        """Function to be called for every message received

        :param DLTMessage message: received new DLTMessage instance
        :returns: True if the loop should continue, False to stop the loop and exit
        :rtype: bool
        """
        self._process_filter_queue()

        if message and (message.apid != "" or message.ctid != ""):
            # Dispatch the message
            msg_ctx = ((message.apid, message.ctid), (None, None), (message.apid, None), (None, message.ctid))
            qids = (
                queue_id
                for filters, queue_ids in self.context_map.items()
                for queue_id in queue_ids
                if filters in msg_ctx
            )
            for queue_id in qids:
                if self.message_queue.full():
                    logger.error("message_queue is full ! put() on this queue will block")
                self.message_queue.put((queue_id, message))

            # Send the message's timestamp
            if self._dlt_time_value:
                self._dlt_time_value.timestamp = message.storage_timestamp

        return not self.mp_stop_flag.is_set()

    def run(self):
        """DLTMessageHandler worker method"""
        if self._filename is not None:
            logger.info("Opening the DLT trace file '%s'", self._filename)
            self.tracefile = open(self._filename, mode="ab", buffering=False)

        while not self.mp_stop_flag.is_set():
            exception_occured = False
            if not self._client_connect():
                # keep trying to reconnect, until we either successfully
                # connect or the stop_flag is set
                continue
            try:
                res = py_dlt_client_main_loop(self._client, verbose=0, callback=self.handle, dumpfile=self.tracefile)
                if res is False and not self.mp_stop_flag.is_set():  # main loop returned False
                    logger.error("DLT connection lost. Restarting DLT client")
                    exception_occured = True
            except KeyboardInterrupt:
                exception_occured = True
                logger.debug("main loop manually interrupted")
                break
            except socket.timeout as exc:
                exception_occured = True
                logger.error("socket timeout error")
                logger.debug(exc)
            except Exception:  # pylint: disable=broad-except
                exception_occured = True
                logger.exception("Exception during the DLT message receive")

            finally:
                if exception_occured:
                    logger.debug("Closing open socket connections.")
                    self._client.disconnect()

        self.message_queue.close()
        self._client.disconnect()
        logger.info("DLTMessageHandler worker execution complete")
