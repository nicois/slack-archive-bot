import os
import logging
import sqlite3
from os.path import join
from jinja2 import Environment, FileSystemLoader, select_autoescape

env = Environment(loader=FileSystemLoader('templates'),
                  autoescape=select_autoescape(['html', 'xml']))

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("main")

conn = sqlite3.connect(os.path.join(os.path.dirname(__file__), 'slack.sqlite'))
conn.row_factory = sqlite3.Row


known_channels = set(record[0] for record in conn.execute(
    "SELECT DISTINCT channel FROM messages"))
channel_map = dict((record['id'], record['name'])
                   for record in conn.execute('SELECT id, name FROM channels'))


template = env.get_template('channel.html')


def dump_channel(channel_id, destination_dir):
    try:
        channel_name = channel_map[channel_id]
    except KeyError:
        logger.warning("Using ID {} instead of its name".format(channel_id))
        channel_name = channel_id
    logger.info("dumping {}".format(channel_name))
    context = dict()
    sql = 'SELECT * from messages WHERE thread_timestamp = timestamp'
    context['threads'] = set(record['timestamp']
                             for record in conn.execute(sql))
    context['messages'] = list(conn.execute('''
SELECT messages.*,
       users.name,
       users.avatar
FROM messages
LEFT OUTER JOIN users ON (messages.user = users.id)
ORDER BY messages.timestamp
'''))
    with open(join(destination_dir,
              "{}.html".format(channel_name)), "wt") as out:
        out.write(template.render(**context))


'''
            format_message(message=message,
                           is_thread=(message['timestamp'] in threads),
                           out=out)


def format_message(message, is_thread, out):
    out.write(''<div>
    {timestamp} - {message}
</div>
'''


if __name__ == '__main__':
    for channel_id in known_channels:
        dump_channel(channel_id=channel_id, destination_dir="/tmp/")
