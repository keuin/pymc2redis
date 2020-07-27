# -------------------------------------------------
# PyMC2Redis: Python Minecraft to Redis script
# Author: Keuin
# Version: 1.0 20.07.27
# Homepage: https://github.com/keuin/pymc2redis
# -------------------------------------------------

import json
import threading
import time
from threading import Lock
from threading import Thread

import redis
from redis import Redis

CONFIG_FILE_NAME = 'pymc2redis.json'
RECEIVE_LOOP_TIMEOUT_SECONDS = 2  # timeout in redis LPOP operation
RECEIVE_LOOP_SLEEP_SECONDS = 0.5  # time in threading.Event.wait()
RETRY_SLOWDOWN_INTERVAL_SECONDS = 20
RETRY_SLOWDOWN_TIMES_THRESHOLD = 10

MSG_PREFIX = [' ', '#']
MSG_ENCODING = 'utf-8'
MSG_COLOR = '§7'
MSG_USER_COLOR = '§d'
MSG_SPLIT_STR = '||'

CFG_REDIS_SERVER = 'redis_server'
CFG_REDIS_SERVER_ADDRESS = 'address'
CFG_REDIS_SERVER_PORT = 'port'
CFG_REDIS_SERVER_PASSWORD = 'password'
CFG_KEY = 'key'
CFG_KEY_SENDER = 'sender'
CFG_KEY_RECEIVER = 'receiver'


# Simple STDOUT logger
def log(text, prefix, ingame):
    message = '[PyMC2Redis][{pf}] {msg}'.format(pf=prefix, msg=text)
    if ingame and svr:
        svr.say('§e{msg}'.format(msg=message))
    print(message)


def info(text, ingame=False):
    log(text, 'INF', ingame)


def warn(text, ingame=False):
    log(text, 'WRN', ingame)


def error(text, ingame=False):
    log(text, 'ERR', ingame)


class MessageThread(Thread):
    __quit_event = threading.Event()

    def quit(self):
        self.__quit_event.set()

    def run(self):
        info('Starting MessageThread...')

        global enabled, con, retry_counter
        while enabled and con:
            try:
                if retry_counter >= RETRY_SLOWDOWN_TIMES_THRESHOLD:
                    time.sleep(RETRY_SLOWDOWN_INTERVAL_SECONDS)
                raw_message = con.brpop(keys=config_keys[CFG_KEY_SENDER], timeout=RECEIVE_LOOP_TIMEOUT_SECONDS)
                if not raw_message:
                    continue
                if len(raw_message) != 2:
                    error('Received invalid message from Redis server. Ignoring. ({})'.format(raw_message))
                    continue
                msg = str(raw_message[1], encoding=MSG_ENCODING)
                print_ingame_message(msg)
                retry_counter = 0  # If we succeed, reset the cool-down counter
            except (ConnectionError, TimeoutError, redis.RedisError) as e:
                print('An exception occurred when waiting for messages from the Redis server: {}'.format(e))
                retry_counter += 1
            self.__quit_event.wait(RECEIVE_LOOP_SLEEP_SECONDS)

        info('Quitting MessageThread...')


# Main program

con = None
enabled = False
config_server = dict()
config_keys = dict()
svr = None
message_thread = None
redis_reconnect_lock = Lock()
retry_counter = 0


def redis_connect():
    global con
    if config_server and CFG_REDIS_SERVER_ADDRESS in config_server and CFG_REDIS_SERVER_PORT in config_server:
        host = config_server[CFG_REDIS_SERVER_ADDRESS]
        port = config_server[CFG_REDIS_SERVER_PORT]
        password = config_server[CFG_REDIS_SERVER_PASSWORD] if CFG_REDIS_SERVER_PASSWORD else None
        con = Redis(
            host=host,
            port=port,
            password=password,
            socket_timeout=10,
            socket_connect_timeout=10,
            health_check_interval=15)
        info("Connected? I'm not sure.")


def redis_reconnect():
    global retry_counter
    try:
        if redis_reconnect_lock.acquire(False):
            if retry_counter >= RETRY_SLOWDOWN_TIMES_THRESHOLD:
                time.sleep(RETRY_SLOWDOWN_INTERVAL_SECONDS)
            warn('Lost connection. Reconnecting to the Redis server...', True)
            time.sleep(1)
            redis_connect()
            retry_counter += 1
    except Exception as e:
        error('Unexpected exception occurred while reconnecting: {}'.format(e))

    redis_reconnect_lock.release()


def init() -> bool:
    """
    Clean-up, load config file and connect to redis server.
    :return: True if success, False if failed to initialize.
    """
    global con, enabled, config_server, config_keys, message_thread

    # reset connection
    if con:
        con.close()
    con = None
    if message_thread and message_thread.is_alive():
        message_thread.quit()
    message_thread = MessageThread()

    # read configuration and connect
    try:
        with open(CONFIG_FILE_NAME, 'r') as f:
            config = json.load(f)
    except FileNotFoundError:
        error('Cannot locate configuration file {}.'.format(CONFIG_FILE_NAME))
        return False

    # --- check config ---

    # check server
    if CFG_REDIS_SERVER not in config:
        error('Cannot read redis server info from {}.'.format(CONFIG_FILE_NAME))
        return False
    config_server = config[CFG_REDIS_SERVER]
    if CFG_REDIS_SERVER_ADDRESS not in config_server \
            or CFG_REDIS_SERVER_PORT not in config_server:
        error('Redis server address or port is not defined. Check {} file.'.format(CONFIG_FILE_NAME))
        return False

    # check keys
    if CFG_KEY not in config:
        error('Cannot read keys from {}.'.format(CONFIG_FILE_NAME))
        return False
    config_keys = config[CFG_KEY]
    if CFG_KEY_SENDER not in config_keys \
            or CFG_KEY_RECEIVER not in config_keys:
        error('Cannot read keys.sender or keys.receiver from the configuration.')
        return False

    # --- check config ---

    # connect to Redis host
    try:
        host = config_server[CFG_REDIS_SERVER_ADDRESS]
        port = config_server[CFG_REDIS_SERVER_PORT]
        password = config_server[CFG_REDIS_SERVER_PASSWORD] if CFG_REDIS_SERVER_PASSWORD else None
        info('Connecting to Redis server, host={host}:{port}, password={pwd}.'.format(host=host, port=port,
                                                                                      pwd=password))
        redis_connect()
    except redis.RedisError as e:
        error('Failed to connect to Redis server:\n{}'.format(e))
        con = None
        return False

    return True


def is_valid_message(s: str) -> bool:
    """
    Check if this string should be transmitted to the Redis server.
    :param s: The string to be checked.
    :return: True or False.
    """
    for pf in MSG_PREFIX:
        if s.startswith(pf):
            return True
    return False


def pack_message(message: str, sender: str) -> str:
    return "{sender}{split}{msg}".format(sender=sender, msg=message, split=MSG_SPLIT_STR)


def clean_message(message: str) -> str:
    for pf in MSG_PREFIX:
        if message.startswith(pf):
            return message[len(pf):]
    return message


def print_ingame_message(msg: str):
    """
    Receive a message from Redis, format it, and print it on the in-game chat menu.
    :param msg: the message with format like "trueKeuin||Hello World!".
    :return: False if the message is invalid.
    """
    sp = str(msg).split(MSG_SPLIT_STR)
    if len(sp) == 2:
        svr.say('{user_color}<{user}>{msg_color}{msg}'.format(msg_color=MSG_COLOR, msg=sp[1], user_color=MSG_USER_COLOR,
                                                              user=sp[0]))


def on_load(server, old_module):
    global enabled, svr, message_thread
    enabled = init()
    svr = server
    if not enabled:
        warn('Due to an earlier error, PyMC2Redis will be disabled. Please check your configuration and reload.')
        return
    message_thread.run()


def on_unload(server):
    global enabled, con, message_thread
    if not enabled:
        return
    enabled = False
    if message_thread:
        message_thread.quit()
    if con:
        con.close()


def on_user_info(server, info_):
    global con
    broken_connection = False
    try:
        msg = str(info_.content)
        player = str(info_.player)

        if is_valid_message(msg):
            if con:
                msg = clean_message(msg)
                info('Pushing: {user}->{msg}'.format(user=player, msg=msg))
                r = con.lpush(config_keys[CFG_KEY_RECEIVER], pack_message(msg, player))
                try:
                    if isinstance(r, bytes) or isinstance(r, bytearray):
                        r = str(r, encoding=MSG_ENCODING)
                    numeric = int(r)
                    if numeric > 0:
                        info('Success.')
                    else:
                        info('Failed when pushing message: queue_length={}, raw_response={}'.format(numeric, r))
                except ValueError:
                    error('Invalid response: {}'.format(r))
            else:
                error('Broken connection. Cannot talk to Redis server.', True)
                broken_connection = True
    except (ConnectionError, TimeoutError, redis.RedisError) as e:
        error('Failed to repeat message to the Redis server: {}.'.format(e))
        broken_connection = True
    except Exception as e:
        error('Unexpected exception: {}'.format(e))

    if broken_connection:
        redis_reconnect()
