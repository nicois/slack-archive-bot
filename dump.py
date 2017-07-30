import os
import logging
import sqlite3
from os.path import join


logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("main")

conn = sqlite3.connect(os.path.join(os.path.dirname(__file__), 'slack.sqlite'))
conn.row_factory = sqlite3.Row


known_channels = set(record[0] for record in conn.execute(
    "SELECT channel, count(*) FROM messages"))
channel_map = dict((record['id'], record['name'])
                   for record in conn.execute('SELECT id, name FROM channels'))


def dump_channel(channel_id, destination_dir):
    try:
        channel_name = channel_map[channel_id]
    except KeyError:
        logger.warning("Using ID {} instead of its name".format(channel_id))
        channel_name = channel_id
    logger.info("dumping {}".format(channel_name))
    sql = 'SELECT * from messages WHERE thread_timestamp = timestamp'
    threads = set(record['timestamp'] for record in conn.execute(sql))
    with open(join(destination_dir,
              "{}.html".format(channel_name)), "wt") as out:
        for message in conn.execute('''
SELECT messages.*,
       users.name,
       users.avatar
FROM messages
LEFT OUTER JOIN users ON (messages.user = users.id)
ORDER BY messages.timestamp
'''):
            format_message(message=message,
                           is_thread=(message['timestamp'] in threads),
                           out=out)


def format_message(message, is_thread, out):
    out.write('''<div>
    {timestamp} - {message}
</div>
'''.format(**message))


if __name__ == '__main__':
    for channel_id in known_channels:
        dump_channel(channel_id=channel_id, destination_dir="/tmp/")
