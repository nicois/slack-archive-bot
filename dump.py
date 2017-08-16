import os
import re
import logging
import sqlite3
import time
from datetime import datetime
from os.path import join
from jinja2 import Environment, FileSystemLoader, select_autoescape

env = Environment(loader=FileSystemLoader('templates'),
                  autoescape=select_autoescape(['html', 'xml']))

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("main")
user_regex = re.compile(r'<@(.+?)[\|>]')
conn = sqlite3.connect(os.path.join(os.path.dirname(__file__), 'slack.sqlite'))
conn.row_factory = sqlite3.Row


known_channels = set(record[0] for record in conn.execute(
    "SELECT DISTINCT channel FROM messages"))
channel_map = dict((record['id'], record['name'])
                   for record in conn.execute('SELECT id, name FROM channels'))

user_map = dict((record['id'], record)
                for record in conn.execute('SELECT * FROM users'))


def format_timestamp(epoch):
    dt = datetime.fromtimestamp(float(epoch))
    return dt.strftime("%c")


def replace_user(matchobj):
    user_id = matchobj.group(1)
    return format_user(user_id)


def format_user(user_id):
    try:
        name = user_map[user_id]['name']
        return f'<span><b>{name}</b></span>'
    except KeyError:
        return 'unknown user ID'


def format_message(message):
    return user_regex.sub(replace_user, message)


env.filters['timestamp'] = format_timestamp
env.filters['message'] = format_message
env.filters['user'] = format_user
template = env.get_template('channel.html')


def dump_channel(channel_id, destination_dir):
    try:
        channel_name = channel_map[channel_id]
    except KeyError:
        logger.warning("Using ID {} instead of its name".format(channel_id))
        channel_name = channel_id
    logger.info("dumping {}".format(channel_name))
    context = dict(bool=bool,
                   time=time,
                   datetime=datetime,
                   round=round,
                   float=float)
    sql = 'SELECT * from messages WHERE thread_timestamp = timestamp'
    context['threads'] = set(record['timestamp']
                             for record in conn.execute(sql))
    context['messages'] = list(conn.execute('''
SELECT messages.*,
       users.name,
       users.avatar
FROM messages
LEFT OUTER JOIN users ON (messages.user = users.id)
ORDER BY COALESCE(thread_timestamp, messages.timestamp), messages.timestamp
'''))
    with open(join(destination_dir,
              "{}.html".format(channel_name)), "wt") as out:
        out.write(template.render(**context))


if __name__ == '__main__':
    for channel_id in known_channels:
        dump_channel(channel_id=channel_id, destination_dir="/tmp/")
