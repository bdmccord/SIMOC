import sys
import json
import time
import random

from celery import Celery
from celery import current_task
from celery.utils.log import get_task_logger
from celery.signals import worker_process_init
from celery.concurrency import asynpool
asynpool.PROC_ALIVE_TIMEOUT = 100.0

logger = get_task_logger(__name__)

sys.path.append("../")

from simoc_abm.agent_model import AgentModel
from simoc_server.database.db_model import User
from simoc_server.exceptions import NotFound
from simoc_server import redis_conn, db

app = Celery('tasks')
app.config_from_object('celery_worker.celeryconfig')


@worker_process_init.connect
def on_worker_init(**kwargs):
    logger.info('New Celery worker initialized.')


def get_user(username, num_retries=30, interval=1):
    while True:
        try:
            user = User.query.filter_by(username=username).all()
        except Exception as err:
            user = []
            logger.error(f'Exception in task.get_user({username=}) ({err}): rolling back')
            db.session.rollback()
        if len(user) != 1:
            logger.warning(f'User {username!r} NOT found ({num_retries=} left): {user=}')
            if num_retries > 0:
                num_retries -= 1
                db.session.rollback()
                time.sleep(interval)
            else:
                raise NotFound(f'User {username} not found.')
        else:
            logger.info(f'User found ({num_retries=} left): {user[0]}')
            return user[0]

BUFFER_SIZE = 100  # Number of steps to execute between adding records to Redis
RECORD_EXPIRE = 1800  # Number of seconds to keep records in Redis

@app.task
def new_game(username, game_config, num_steps, expire=3600):
    logger.info(f'Starting new game for {username=}, {num_steps=}')
    # Initialize model
    user = get_user(username)
    game_id = random.getrandbits(63)
    model = AgentModel.from_config(**game_config, record_initial_state=False)
    model.game_id = game_id
    model.user_id = user.id
    # Save complete game config to Redis
    complete_game_config = model.save()
    redis_conn.set(f'game_config:{game_id}', json.dumps(complete_game_config), ex=expire)
    # Initialize Redis
    logger.info(f'Setting user:{user.id} task:{game_id:X} on Redis')
    redis_conn.set(f'task_mapping:{game_id}', new_game.request.id, ex=expire)
    redis_conn.set(f'user_mapping:{user.id}', game_id, ex=expire)
    key = f'records:{game_id}'
    try:
        # Run the model and add records to Redis
        batch_num = 0
        while model.step_num <= num_steps and not model.is_terminated:
            start_time = time.time()
            n_steps = min(BUFFER_SIZE, num_steps - model.step_num)
            for _ in range(n_steps):
                model.step()
            records = model.get_records(static=True, clear_cache=True)
            # Include the number of steps so views.py knows when it's finished
            records['n_steps'] = n_steps
            redis_conn.rpush(key, json.dumps(records))
            batch_num += 1
            elapsed_time = time.time() - start_time
            logger.info(f'Added batch {batch_num} ({n_steps} records) to Redis for {user.id}:{game_id} in {elapsed_time:.3f} seconds')
        logger.info(f'Game {game_id:X} finished successfully after {model.step_num} steps')

    finally:
        redis_conn.expire(key, RECORD_EXPIRE)
        logger.info(f'Completed simulation for {user}')
