#!/usr/bin/env python
# -*- coding: UTF-8 -*-

import hypchat
import time
from datetime import datetime
import yaml
import shlex
import coloredlogs
import logging
import Queue
import threading
from slackclient import SlackClient
import json
import requests
import re
from argparse import ArgumentParser
from prometheus_client import start_http_server, Counter
from bs4 import BeautifulSoup

# fix SSL warning
import requests.packages.urllib3
requests.packages.urllib3.disable_warnings()

# logger
logger = logging.getLogger("bot")
logger.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
# end logger

"""
Initialization of several counters to monitor this application through Prometheus.
"""
read_msg_counter = Counter('im_pipe_messages_read', 'Number of messages read', ['adapter', 'source'])
scrap_counter = Counter('im_pipe_scraps', 'Number of time a source has been scrapped', ['adapter', 'source'])
sent_msg_counter = Counter('im_pipe_messages_sent', 'Number of messages sent', ['adapter', 'destination'])

coloredlogs.install()

class Adapter:
    """
    This class gather some global functions which will be apply by all adapter (slack,flowdock,hipchat).
    """
    def __init__(self, *args, **kwargs):
        pass

    def enqueue(self, queue, message):
        """
        This function allows to put a message into a specific queue.
        It takes two arguments :
          - queue : Queue object
          - message : a message to save into the queue given.
        """
        logger.info(message)
        queue.put(message)

    def match_pattern(self, patterns, msg):
        """
        If an include pattern is set into the configuration file, this function allows to check if we have
        a matching between this pattern and a message.
        """
        for pattern in patterns:
            m = re.match(pattern, msg)
            if m:
                return True
        return False


class FlowdockAdapter(Adapter):
    """
    This class allows to carry out many actions on a flowdock instance through his API.
    It returns an Adapter object type.
    """
    def __init__(self, *args, **kwargs):
        """
        This constructor allows to initialize an Adapter object and to prepare a connexion to a flowdock
        server using his API and a authentication method by token.

        It takes two arguments :
          - *args : Allows to pass multiple non-keyworded variable.
          - **kwargs : Allows to pass multiple keyworded variable.
        """
        self.server = kwargs['server']
        self.token = kwargs['token']

    def format_message(self, msg):
        # flowdock emoji format is :word:
        # replace hipchat (troll)
        msg = re.sub(r'\(([^\)]+)\)', r':\1:', msg)
        return msg

    def send_messages(self, queues):
        """
        Thanks to the flowdock API, this function allows to send all messages saved into a queue
        to another team commmunication tool such as Slack or Hipchat.

        It takes a single argument :
         - queues : List which contains all the queues.
        """
        for q in queues:
            queue = q['queue']
            try:
                m = queue.get(block=False)
                org, flow = q['dest_channel'].split('|')
                url = '{server}/flows/{org}/{flow}/messages'.format(
                    server=self.server,
                    org=org,
                    flow=flow,
                )
                auth = (self.token, '')
                payload = {
                    'event': 'message',
                    'content': self.format_message(m),
                }
                headers = {
                    'Content-Type': 'application/json'
                }
                r = requests.post(url,
                                  data=json.dumps(payload),
                                  auth=auth,
                                  headers=headers)
                if not r.status_code == 201:
                    raise Exception(r.text)
                sent_msg_counter.labels('flowdock', q['dest_channel']).inc()
                queue.task_done()
            except Queue.Empty:
                pass


class HipchatAdapter(Adapter):
    """
    This class allows to carry out many actions on a hipchat instance through his API.
    It returns an Adapter object type.
    """
    def __init__(self, *args, **kwargs):
        """
        This constructor allows to initialize an Adapter object and a connexion to a hipchat
        server using his API and a authentication method.

        It takes two arguments :
          - *args : Allows to pass multiple non-keyworded variable.
          - **kwargs : Allows to pass multiple keyworded variable.
        """
        self.server = kwargs['server']
        self.token = kwargs['token']
        self.connection = self.connect()

    def connect(self):
        """
        This function allows to create a connexion to a hipchat server through his API.
        """
        return hypchat.HypChat(self.token, endpoint=self.server)

    def get_messages(self, channel):
        """
        Thanks to the hipchat API, this function allows to retrieve all messages sent into a specific channel and,
        save them into a dedicated queue.

        It takes a single argument :
          - channel : Hipchat channel name.
        """
        # needed to avoid API rate limits
        time.sleep(10)

        try:
            room = self.connection.get_room(channel.name)
        except hypchat.requests.HttpNotFound as e:
            logger.error(
                "room %s at %s not found" % (channel.name, self.server))
            return None
        except requests.exceptions.ConnectionError as e:
            self.connection = hypchat.HypChat(self.token, endpoint=self.server)
            room = self.connection.get_room(channel.name)
        except hypchat.requests.HttpGatewayTimeout as e:
            self.connection = hypchat.HypChat(self.token, endpoint=self.server)
            room = self.connection.get_room(channel.name)
        try:
            messages = list(room.history(maxResults=90).contents())
        except hypchat.requests.HttpGatewayTimeout as e:
            logger.error(e)
            return
        old_cursor = channel.cursor
        logger.info(
            "Fetching message from %s (%s)" % (channel.name, self.server))
        scrap_counter.labels('hipcat', room['name']).inc()
        for message in messages:
            d = message['date']
            message_date = datetime(
                d.year, d.month, d.day,
                d.hour, d.minute, d.second, d.microsecond,
                None
                )
            if message_date <= old_cursor:
                continue
            if message_date > old_cursor:
                old_cursor = message_date
            if type(message['from']) == unicode:
                msg = "%s@%s | %s" % \
                    (message['from'], channel.name, message['message'])
            else:
                msg = "%s@%s | %s" % \
                        (message['from']['name'],
                         channel.name, message['message'])
            if channel.include_pattern and \
                    not self.match_pattern(
                            channel.include_pattern, message['message']):
                msg = 'Message skipped as not in include_pattern'
                logger.info(msg)
                channel.cursor = old_cursor
                continue
            self.enqueue(queue=channel.queue, message=msg)
            read_msg_counter.labels('hipchat', room['name']).inc()
        channel.cursor = old_cursor

    def send_messages(self, queues):
        """
        Thanks to the hipchat API, this function allows to send all messages saved into a queue
        to another team commmunication tool such as Slack or Flowdock.

        It takes a single argument :
         - queues : List which contains all the queues.
        """
        time.sleep(10)
        for q in queues:
            queue = q['queue']
            try:
                m = queue.get(block=False)

                try:
                    room = self.connection.get_room(q['dest_channel'])
                except Exception as e:
                    logger.exception(e)
                    self.connect()
                    return
                room.notification(m)
                sent_msg_counter.labels('hipchat', room['name']).inc()
                queue.task_done()
            except Queue.Empty:
                pass


class SlackAdapter(Adapter):
    """
    This class allows to carry out many actions on a slack instance through his API.
    It returns an Adapter object type.
    """
    def __init__(self, *args, **kwargs):
        """
        This constructor allows to initialize an Adapter object and a connexion to a slack
        server using his API and a authentication method.

        It takes two arguments :
          - *args : Allows to pass multiple non-keyworded variable.
          - **kwargs : Allows to pass multiple keyworded variable.
        """
        self.server = kwargs['server']
        self.token = kwargs['token']
        self.connection = SlackClient(self.token)

    def get_user(self, user_id):
        """
        Thanks to a simple call on the slack API, this function allows to get informations
        about a user.

        This function takes a single argument :
          - user_id : Slack user ID.
        """
        raw = self.connection.api_call('users.info', user=user_id)
        resp = json.loads(json.dumps(raw))
        return resp['user']

    def get_channel(self, channel):
        """
        Thanks to a simple call on the slack API, this function allows to get a list of all channel that exists
        and verify if the channel name which is given in argument is registered into the channel list.

        This function takes a single argument :
          - channel : Slack channel name
        """

        raw = self.connection.api_call('channels.list', exclude_archived=1)
        resp = json.loads(json.dumps(raw))

        for c in resp['channels']:
            if c['name'] == channel.name:
                return c
        logger.debug(
            'Public channel %s not found!' % channel.name)

        raw = self.connection.api_call('groups.list')
        resp = json.loads(json.dumps(raw))
        for c in resp['groups']:
            if c['name'] == channel.name:
                return c
        logger.debug(
            'Private channel %s not found!' % channel.name)
        raise ValueError('Channel %s not found !' % channel.name)

    def get_messages(self, channel):
        """
        Thanks to a simple call on the slack API, this function allows to retrieves and save into a queue
        the messages sent into a channel.

        This function takes a single argument :
          - channel : Channel name.
        """

        def datetime_to_ts(date):
            return (date - datetime(1970, 1, 1)).total_seconds()

        def ts_to_datetime(ts):
            return datetime.fromtimestamp(float(ts))
        try:
            _channel = self.get_channel(channel)
        except ValueError as e:
            logger.error("channel %s at %s not found" %
                         (channel.name, self.server))
            return None
        logger.info("Fetching message from %s (%s)" %
                    (channel.name, self.server))
        if 'is_group' in _channel and _channel['is_group']:
            api_uri = 'groups.history'
        else:
            api_uri = 'channels.history'

        if channel.cursor_ts == 0:
            channel.cursor_ts = datetime_to_ts(channel.cursor)

        try:
            raw = self.connection.api_call(
                api_uri,
                channel=_channel['id'],
                oldest=channel.cursor_ts)
        except Exception as e:
            logger.exception(e)
            return
        resp = json.loads(json.dumps(raw))
        old_cursor = channel.cursor_ts
        scrap_counter.labels('slack', channel.name).inc()
        for message in resp['messages']:
            d = message['ts']
            message_date = ts_to_datetime(d)  # FIXME: can we safely remove this unused variable ?

            if d <= old_cursor:
                continue
            if d > old_cursor:
                old_cursor = d
            if message['type'] == 'message':
                try:
                    user = self.get_user(message['user'])
                    userName = user['name']
                except:
                    userName = message['username']
                msg = "%s@%s | %s" % \
                    (userName, channel.name, BeautifulSoup(message['text'], "html.parser").text)
                self.enqueue(queue=channel.queue, message=msg)
                read_msg_counter.labels('slack', channel.name).inc()
        channel.cursor_ts = old_cursor

    def send_messages(self, queues):
        """
        This function allows to forward all messages saved into a queue to another team communication tool
        such as Hipchat or Flowdock

        It takes a single argument :
				 - queues : List of all queues
        """

        for q in queues:
            queue = q['queue']
            logger.debug("dest_channel is %s" % q['dest_channel'])
            logger.debug("token is %s" % self.token)
            try:
                m = queue.get(block=False)

                try:
                    self.connection.api_call(
                        'chat.postMessage',
                        channel=q['dest_channel'],
                        text=m)
                    sent_msg_counter.labels('slack', q['dest_channel']).inc()
                except Exception as e:
                    logger.exception(e)
                    return

                queue.task_done()
            except Queue.Empty:
                pass


class Writer(threading.Thread):
    """
    This class allows to forward all the messages present in a queue to a specific channel into another team communication tool.
    It returns a thread object type.
    """

    queues = []

    def __init__(self, *args, **kwargs):
        """
        This function is a constructor which retrieves configuration elements present in the write part of the YAML config file (see example below)
        and initialize a thread ready to run.

        Configuration file example (read part) :

          [...]
          - write: backend=<hipchat|slack|flowdock> alias=<ALIAS> server=https://<SERVER_NAME> api_token=<API_TOKEN>
          [...]
        """
        threading.Thread.__init__(self)
        self.kill_received = False
        self.backend = kwargs['backend']
        self.alias = kwargs['alias']
        assert self.backend in ['hipchat', 'flowdock', 'slack']
        if self.backend == 'hipchat':
            self.server = kwargs['server']
            self.token = kwargs['api_token']
            self.adapter = HipchatAdapter(token=self.token, server=self.server)
        if self.backend == 'flowdock':
            self.server = kwargs['server'] \
                    if 'server' in kwargs else 'https://api.flowdock.com'
            self.token = kwargs['api_token']
            self.adapter = FlowdockAdapter(token=self.token,
                                           server=self.server)
        if self.backend == 'slack':
            self.server = kwargs['server'] \
                    if 'server' in kwargs else 'https://slack.com'
            self.token = kwargs['api_token']
            self.adapter = SlackAdapter(token=self.token, server=self.server)

    def run(self):
        """
        This function allows to run in daemon mode each thread previously initialized until a key interrupt is received.
        Each message present into a queue will be sent to a specific team communication tool configured in backend (write part).
        """
        while not self.kill_received:
            try:
                self.adapter.send_messages(self.queues)
            except Exception as e:
                logger.exception(e)
            time.sleep(1)
        msg = "KILL writer"
        logger.warning(msg)


class Reader(threading.Thread):
    """
    This class allows to retrieves all the messages that was send in three team comminucation tools : Slack,Hipchat and Flowdock.
    It returns an object of thread type
    """
    def __init__(self, *args, **kwargs):
        """
        This function is a constructor which retrieves configuration elements present in the read part of the YAML config file (see example below)
        and initialize a thread ready to run.

        Configuration file example (read part) :

          [...]
          - read: backend=<slack|flowdock|hipchat> alias=<ALIAS> server=https://<SERVER_NAME> api_token=<API_TOKEN>
            channels:
             - name: "CHANNEL_NAME"
               forward_to: "<HIPCHAT_ROOM>@<WRITER_NAME> || <FLOWDOCK_ORG>|<FLOWDOCK_FLOW>@<WRITER_NAME> || <SLACK_GROUP_NAME>|<SLACK_CHANNEL_NAME>@<WRITER_NAME>"
               include_pattern:
                 - "FILTERING_PARAMETER" ex(^@all)
          [...]
        """
        threading.Thread.__init__(self)
        self.kill_received = False
        self.backend = kwargs['backend']
        self.channels = []
        self.cursor = datetime.utcnow()
        assert self.backend in ['hipchat', 'slack']
        if self.backend == 'hipchat':
            self.server = kwargs['server']
            self.token = kwargs['api_token']
            self.adapter = HipchatAdapter(token=self.token,
                                          server=self.server,
                                          channels=self.channels)
        if self.backend == 'slack':
            self.server = 'api.slack.com'
            self.token = kwargs['api_token']
            self.adapter = SlackAdapter(token=self.token,
                                        server=self.server,
                                        channels=self.channels)

    def run(self):
        """
        This function allows to run in daemon mode each thread previously initialized until a key interrupt is received.
        For each channel registered into the YAML config file, this function checks if a message was send in a channel and retrieves it into a queue.
        """
        while not self.kill_received:
            for channel in self.channels:
                try:
                    messages = self.adapter.get_messages(channel)
                except Exception as e:
                    logger.exception(e)
                    continue
            time.sleep(10)
        msg = "KILL reader"
        logger.warning(msg)


class Channel:
    """
    This Class allows to retrieve informations about channels which are registered into the YAML
    configuration file.
    """
    def __init__(self, name, forward_to=None, include_pattern=None):
        """
        This function is a constructor which initialize an object of type Channel. It takes three arguments :
          - name : Name of source channel.
          - forward_to : Name of destination channel (forward).
          - include_pattern : An include pattern to filter messages.
        """
        self.cursor = datetime.utcnow()
        self.cursor_ts = 0
        self.name = name
        self.forward_to = forward_to
        self.include_pattern = include_pattern


def main(config):
    threads = []
    queues = []
    for item in config:
        assert 'read' in item or 'write' in item
        if 'read' in item:
            payload = dict(token.split('=') for token in shlex.split(item['read']))
            reader = Reader(**payload)
            assert 'channels' in item
            for channel in item['channels']:
                c = Channel(**channel)
                # Initialize a queue with unlimited capacity (maxsize=0)
                q = Queue.Queue()
                c.queue = q
                queues.append({
                    'name': channel['forward_to'],
                    'dest_channel': channel['forward_to'].split('@')[0],
                    'writer_alias': channel['forward_to'].split('@')[1],
                    'queue': q,
                    })
                reader.channels.append(c)
            t = reader
            t.daemon = True
            threads.append(t)
            t.start()
            msg_channels = [channel.name for channel in reader.channels]
            msg = "Starting read thread-%d (server:%s, channels:%s)" % (threads.index(t), reader.server, ', '.join(msg_channels))
            logger.info(msg)
    for item in config:
        assert 'read' in item or 'write' in item
        if 'write' in item:
            payload = dict(token.split('=') for token in shlex.split(item['write']))
            writer = Writer(**payload)
            writer.queues = [queue for queue in queues if queue['writer_alias'] == writer.alias]
            t = writer
            t.daemon = True
            threads.append(t)
            t.start()
            msg = "Starting write thread-%d (server: %s, channels: %s)" % (
                threads.index(t),
                writer.server,
                ', '.join([q['dest_channel'] for q in writer.queues])
                )
            logger.info(msg)
    while len(threads) > 0:
        try:
            threads = [t.join(3600*24*365*100) for t in threads if t is not None and t.isAlive()]
        except KeyboardInterrupt:
            logger.warning("Ctrl-c received! Sending kill to threads...")
            for t in threads:
                t.kill_received = True

if __name__ == '__main__':
    parser = ArgumentParser(description="Forward IM from and to Slack, Hipchat and Flowdock")
    parser.add_argument('--config', '-c', default='config.yml',
            help='Path to to config file to use.')
    parser.add_argument('--metrics-port', '-p', default=8000,
            type=int, help='Port on which to expose metrics')
    args = parser.parse_args()
    logger.debug('Using config file {}'.format(args.config))
    with open(args.config) as fh:
        config = yaml.load(fh.read())
    start_http_server(args.metrics_port)
    main(config=config)
