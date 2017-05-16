#!/usr/bin/env python2.7

import getpass
import glob
import logging
import os
import re

import yaml

import tornado.gen
import tornado.ioloop
import tornado.options
import tornado.tcpclient

# Regular Expressions ----------------------------------------------------------

PING_RE     = re.compile('^PING (?P<payload>.*)')
CHANMSG_RE  = re.compile(':(?P<nick>.*?)!\S+\s+?PRIVMSG\s+(?P<channel>#+[-\w]+)\s+:(?P<message>[^\n\r]+)')
PRIVMSG_RE  = re.compile(':(?P<nick>.*?)!\S+\s+?PRIVMSG\s+[^#][^:]+:(?P<message>[^\n\r]+)')

# Bobbit -----------------------------------------------------------------------

class Bobbit(object):

    def __init__(self, config_path=None, **kwargs):
        self.logger      = logging.getLogger()
        self.tcp_client  = tornado.tcpclient.TCPClient()
        self.handlers    = [
             (PING_RE   , self.handle_ping),
             (CHANMSG_RE, self.handle_channel_message),
             (PRIVMSG_RE, self.handle_private_message),
        ]

        self.load_configuration(config_path)
        self.load_modules()

    # Connect ------------------------------------------------------------------

    @tornado.gen.coroutine
    def connect(self):
        ''' Connect to IRC server, authorize, register, and identify '''
        self.logger.info('Connecting to %s:%d', self.host, self.port)
        self.tcp_stream  = yield self.tcp_client.connect(self.host, self.port)

        # Send connection password (e.g. Slack)
        if self.password.startswith('CONN:'):
            password = self.password[5:]
            self.logger.info('Sending Connection Password: %s', password)
            self.send('PASS {}'.format(password))

        # Authorize
        self.logger.info('Authorizing as %s', self.nick)
        self.send('USER {} {} bobbit :{}'.format(self.nick, self.host, self.nick))

        # Register
        self.logger.info('Registering as %s', self.nick)
        self.send('NICK {}'.format(self.nick))

        # Identify
        self.logger.info('Identifying as %s', self.nick)
        if not self.password.startswith('CONN:'):
            self.send_message('IDENTIFY {}'.format(self.password), nick='NickServ')

        # Join channels
        for channel in self.channels:
            self.send('JOIN {}'.format(channel))

        # Wait for next message
        self.recv_message('')

    # Send / receive messages --------------------------------------------------

    @tornado.gen.coroutine
    def send(self, message):
        yield self.tcp_stream.write(message + '\r\n')

    def send_message(self, message, channel=None, nick=None):
        if channel:
            receiver = channel
        elif nick:
            receiver = nick
        else:
            receiver = None

        if receiver:
            self.send('PRIVMSG {} :{}'.format(receiver, message))
        else:
            self.logging.warn('No channel or nick specified for: %s', message)

    def send_response(self, response, nick=None, channel=None):
        if response is None or (nick is None and channel is None):
            return

        if isinstance(response, unicode) or isinstance(response, str):
            self.send_message(response, nick, channel)
        else:
            for r in response:
                self.send_response(r, nick, channel)

    def recv_message(self, message):
        # Receive message
        message = message.rstrip()
        self.logger.info(message)

        # Process handlers
        for pattern, callback in self.handlers:
            match = pattern.match(message)
            if match:
                try:
                    callback(**match.groupdict())
                except Exception as e:
                    self.logger.exception('Unhandled exception: %s' % e)

        # Wait for next message
        self.tcp_stream.read_until('\n', self.recv_message)

    # Handlers -----------------------------------------------------------------

    def handle_ping(self, payload):
        self.logger.info('Handling PING: %s', payload)
        self.send('PONG {}'.format(payload))

    def handle_channel_message(self, nick, channel, message):
        self.logger.info('Handling Channel Message: %s | %s | %s', channel, nick, message)
        for response in self.process_command(nick, message, channel):
            self.send_response(response, channel=channel)

    def handle_private_message(self, nick, message):
        self.logger.info('Handling Private Message: %s | %s', nick, message)
        for response in self.process_command(nick, message):
            self.send_response(response, nick=nick)

    # Modules ------------------------------------------------------------------

    def load_modules(self):
        self.logger.info('Importing modules from %s', self.modules_dir)

        # Clear current list of modules and commands
        self.modules  = {}
        self.commands = []

        # Iterate over modules in directory
        for module_path in glob.glob('{}/*.py'.format(self.modules_dir)):
            module_name = module_path[:-3].replace('/', '.').replace('..', '')

            if '__' in module_name:
                continue

            # Load or re-load module
            try:
                self.logger.info('Loading %s', module_name)
                if module_name in self.modules:
                    module = self.modules[module_name]
                    reload(module)
                else:
                    module = __import__(module_name, globals(), locals(), -1)
                    self.modules[module_name] = module
            except ImportError as e:
                self.logger.warn('Failed to import module %s: %s', module_name, e)
                continue

            # Enable module
            try:
                if module.TYPE == 'command':
                    self.commands.extend(module.register(self))
            except Exception as e:
                self.logger.info('Failed to enable module %s: %s', module_name, e)

    def process_command(self, nick, message, channel=None):
        for pattern, callback in self.commands:
            match = pattern.match(message)
            if match:
                yield callback(self, nick, message, channel, **match.groupdict())
    
    # Utilities ----------------------------------------------------------------

    def format_responses(self, responses, nick=None, channel=None):
        prefix = self.nick_prefix
        if isinstance(responses, unicode) or isinstance(responses, str):
            yield u'{}{}: {}'.format(prefix, nick, responses) if channel else responses
        else:
            for response in responses:
                self.format_responses(response, nick, channel)

    # Configuration ------------------------------------------------------------

    def load_configuration(self, config_path=None):
        ''' Load configuration from YAML file '''
        self.work_dir    = os.environ.get('BOBBIT_DIR', os.path.expanduser('~/.config/bobbit'))
        self.config_path = config_path if config_path else os.path.join(self.work_dir, 'config.yaml')
        self.modules_dir = os.path.join(os.path.dirname(__file__), 'modules')

        if os.path.exists(self.config_path):
            config       = yaml.load(open(self.config_path))
            self.work_dir= os.path.dirname(self.config_path)
        else:
            config       = {}

        self.logger.info('Working Directory:  %s', self.work_dir)
        self.logger.info('Configuration Path: %s', self.config_path)
        self.logger.info('Modules Path:       %s', self.modules_dir)

        self.host        = config.get('host'       , 'irc.freenode.net')
        self.port        = config.get('port'       , 6667)
        self.owner       = config.get('owner'      , getpass.getuser())
        self.nick        = config.get('nick'       , 'bobbit')
        self.nick_prefix = config.get('nick_prefix', '')
        self.password    = config.get('password'   , '')
        self.channels    = config.get('channels'   , [])

        self.logger.info('IRC Server:         %s:%d', self.host, self.port)
        self.logger.info('IRC Owner:          %s'   , self.owner)
        self.logger.info('IRC Nick:           %s'   , self.nick)
        self.logger.info('IRC Nick Prefix:    %s'   , self.nick_prefix)
        self.logger.info('IRC Password:       %s'   , self.password)
        self.logger.info('IRC Channels:       %s'   , ', '.join(self.channels))

    # Run ----------------------------------------------------------------------

    def run(self):
        self.connect()
        tornado.ioloop.IOLoop.current().start()

# Main Execution ---------------------------------------------------------------

if __name__ == '__main__':
    tornado.options.define('config_path', default=None,  help='Configuration path')
    tornado.options.parse_command_line()

    options = tornado.options.options.as_dict()
    bobbit  = Bobbit(**options)
    bobbit.run()

# vim: set sts=4 sw=4 ts=8 expandtab ft=python: