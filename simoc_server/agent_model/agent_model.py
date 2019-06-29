r"""Describes Agent Model interface and behaviour,
"""

import datetime
import json
import random
from abc import ABCMeta, abstractmethod

import numpy as np
import quantities as pq
from mesa import Model
from mesa.space import MultiGrid
from mesa.time import RandomActivation
from sqlalchemy.orm.exc import StaleDataError

from simoc_server import db, app
from simoc_server.agent_model.agents.core import GeneralAgent, StorageAgent
from simoc_server.agent_model.attribute_meta import AttributeHolder
from simoc_server.database.db_model import AgentModelParam, AgentType, AgentModelState, \
    AgentModelSnapshot, SnapshotBranch, CurrencyType
from simoc_server.util import timedelta_to_hours, location_to_day_length_minutes


class PrioritizedRandomActivation(RandomActivation):
    """A custom step scheduler for MESA prioritized by agent class."""

    def __init__(self, model):
        """Creates a MESA agent scheduler object.

        A scheduler manages agent activations for each time step. This class performs step method
        execution of all agents in the predefined order based on their classes. All activations
        within a single class are scheduled randomly. This provides an ability to prioritize
        currency exchange for specific classes of agents (e.g. humans).

        Args:
          model: mesa.Model, MESA model to manage
        """
        super(RandomActivation, self).__init__(model)

    def step(self):
        agent_by_class = {}
        for agent in self.agents[:]:
            agent_class = AgentType.query.get(agent.agent_type_id).agent_class
            if agent_class not in agent_by_class:
                agent_by_class[agent_class] = []
            agent_by_class[agent_class].append(agent)
        for agent_class in self.model.priorities:
            if agent_class in agent_by_class:
                agents = agent_by_class[agent_class]
                self.model.random_state.shuffle(agents)
                for agent in agents[:]:
                    agent.step()
        self.steps += 1
        self.time += 1


class AgentModel(Model, AttributeHolder):
    """The core class that describes the SIMOC's Agent Model interface.

    The class stores and manages a stateful representation of a single SIMOC simulation and takes
    care of all agent management and orchestration, model initialization, persistence and
    monitoring.

    Attributes:
          config: int,
          day_length_hours: int,
          day_length_minutes: int,
          daytime: int,
          grid: int,
          grid_height: int,
          grid_width: int,
          is_terminated: int,
          location: int,
          minutes_per_step: int,
          storage_ratios: Dict,
          priorities: int,
          random_state: int,
          scheduler: int,
          seed: int,
          single_agent: int,
          snapshot_branch: int,
          termination: int,
          time: int,
    """

    def __init__(self, init_params):
        """Creates an Agent Model object.

        TODO

        Args:
          init_params: AgentModelInitializationParams, TODO
        """
        super(Model, self).__init__()
        self.load_params()
        self.user_id = None
        self.grid_width = init_params.grid_width
        self.grid_height = init_params.grid_height
        self.snapshot_branch = init_params.snapshot_branch
        self.seed = init_params.seed
        self.random_state = init_params.random_state
        self.termination = init_params.termination
        self.termination_reason = None
        self.single_agent = init_params.single_agent
        self.priorities = init_params.priorities
        self.location = init_params.location
        self.config = init_params.config
        self.time = init_params.starting_model_time
        self.minutes_per_step = init_params.minutes_per_step
        self.is_terminated = False
        self.storage_ratios = {}
        self.step_records_buffer = []
        self.grid = MultiGrid(self.grid_width, self.grid_height, True)
        self.day_length_minutes = location_to_day_length_minutes(self.location)
        self.day_length_hours = self.day_length_minutes / 60
        self.daytime = int(self.time.total_seconds() / 60) % self.day_length_minutes
        if self.seed is None:
            self.seed = int(np.random.randint(2 ** 32, dtype='int64'))
        if self.random_state is None:
            self.random_state = np.random.RandomState(self.seed)
        if self.priorities:
            self.scheduler = PrioritizedRandomActivation(self)
        else:
            self.scheduler = RandomActivation(self)
        self.scheduler.steps = init_params.starting_step_num

    @property
    def logger(self):
        """Returns Flask logger object."""
        return app.logger

    def get_step_logs(self):
        """TODO

        Called from:
            game_runner.py GameRunner.step_to.step_loop

        Returns:
        """
        record_id = random.randint(1, 1e7)
        model_record = dict(id=record_id,
                            step_num=self.step_num,
                            user_id=self.user_id,
                            time=self["time"].total_seconds(),
                            hours_per_step=timedelta_to_hours(self.timedelta_per_step()),
                            is_terminated=str(self.is_terminated),
                            termination_reason=self.termination_reason)
        agent_type_counts = []
        for agent_type_id, counter in self.get_agent_type_counts().items():
            agent_type_count_record = dict(model_record_id=record_id,
                                           agent_type_id=agent_type_id,
                                           agent_counter=counter)
            agent_type_counts.append(agent_type_count_record)
        storage_capacities = []
        for storage in self.get_storage_capacities():
            for currency in storage['currencies']:
                currency_type = CurrencyType.query.filter_by(name=currency['name']).first()
                storage_capacity_record = dict(model_record_id=record_id,
                                               agent_type_id=storage['agent_type_id'],
                                               agent_id=storage['agent_id'],
                                               storage_id=storage['storage_id'],
                                               currency_type_id=currency_type.id,
                                               value=currency['value'],
                                               capacity=currency['capacity'],
                                               units=currency['units'])
                storage_capacities.append(storage_capacity_record)
        return model_record, agent_type_counts, storage_capacities

    def get_agent_type_counts(self):
        """TODO

        TODO

        Returns:
          TODO
        """
        counter = {}
        for agent in self.get_agents_by_class(agent_class=GeneralAgent):
            agent_type_id = agent.agent_type_id
            if agent_type_id not in counter:
                counter[agent_type_id] = 0
            counter[agent_type_id] += 1 * agent.amount
        return counter

    def get_storage_capacities(self):
        """TODO

        Formats the agent storages and currencies for easier access to the step information later.

        Returns:
          A dictionary of the storages information for this step
        """
        storages = []
        for storage in self.get_agents_by_class(agent_class=StorageAgent):
            entity = {"agent_type_id": storage.agent_type_id,
                      "agent_id": storage.unique_id,
                      "storage_id": storage.id,
                      "currencies": []}
            for attr in storage.attrs:
                if attr.startswith('char_capacity'):
                    currency = attr.split('_', 2)[2]
                    entity["currencies"].append({"name": currency,
                                                 "value": storage[currency],
                                                 "capacity": storage.attrs[attr],
                                                 "units": storage.attr_details[attr]['units']})
            storages.append(entity)
        return storages

    @property
    def step_num(self):
        """Returns the last step number."""
        return self.scheduler.steps

    def load_params(self):
        """TODO"""
        params = AgentModelParam.query.all()
        for param in params:
            value_type_str = param.value_type
            if value_type_str != type(None).__name__:
                value_type = eval(value_type_str)
                self.__dict__[param.name] = value_type(param.value)
            else:
                self.__dict__[param.name] = None

    @classmethod
    def load_from_db(cls, agent_model_state):
        """TODO

        TODO

        Args:
            agent_model_state: simoc_server.database.db_model.AgentModelState, Agent model state to
                load the game runner from.

        Returns:
          TODO
        """
        snapshot_branch = agent_model_state.agent_model_snapshot.snapshot_branch
        grid_width = agent_model_state.grid_width
        grid_height = agent_model_state.grid_height
        step_num = agent_model_state.step_num
        model_time = agent_model_state.model_time
        seed = agent_model_state.seed
        random_state = agent_model_state.random_state
        minutes_per_step = agent_model_state.minutes_per_step
        location = agent_model_state.location
        termination = json.loads(agent_model_state.termination)
        priorities = json.loads(agent_model_state.priorities)
        config = json.loads(agent_model_state.config)
        init_params = AgentModelInitializationParams()
        (init_params.set_grid_width(grid_width)
         .set_grid_height(grid_height)
         .set_starting_step_num(step_num)
         .set_starting_model_time(model_time)
         .set_snapshot_branch(snapshot_branch)
         .set_seed(seed)
         .set_random_state(random_state)
         .set_termination(termination)
         .set_priorities(priorities)
         .set_minutes_per_step(minutes_per_step)
         .set_location(location)
         .set_config(config))
        model = AgentModel(init_params)
        agents = {}
        for agent_state in agent_model_state.agent_states:
            agent_class = agent_state.agent_type.agent_class
            if agent_class not in agents:
                agents[agent_class] = []
            agents[agent_class].append({"agent_type": agent_state.agent_type.name,
                                        "unique_id": agent_state.agent_unique_id,
                                        "model_time_created": agent_state.model_time_created,
                                        "id": agent_state.agent_id,
                                        "active": agent_state.active,
                                        "age": agent_state.age,
                                        "amount": agent_state.amount,
                                        "lifetime": agent_state.lifetime,
                                        "connections": json.loads(agent_state.connections),
                                        "buffer": json.loads(agent_state.buffer),
                                        "deprive": json.loads(agent_state.deprive),
                                        "attributes": json.loads(agent_state.attributes)})
        for storage in agents['storage']:
            agent = StorageAgent(model=model, **storage)
            for attr in storage['attributes']:
                agent[attr['name']] = attr['value']
            model.add_agent(agent)
        _ = agents.pop('storage')
        for agent_class in agents:
            for agent in agents[agent_class]:
                new_agent = GeneralAgent(model=model, **agent)
                for attr in agent['attributes']:
                    new_agent[attr['name']] = attr['value']
                model.add_agent(new_agent)
        return model

    @classmethod
    def create_new(cls, model_init_params, agent_init_recipe):
        """TODO

        TODO

        Args:
            model_init_params: TODO
            agent_init_recipe: TODO

        Returns:
          TODO
        """
        model = AgentModel(model_init_params)
        agent_init_recipe.init_agents(model)
        return model

    def add_agent(self, agent, pos=None):
        """TODO

        TODO

        Args:
            agent: TODO
            pos: TODO
        """
        if pos is None and hasattr(agent, "pos"):
            pos = agent.pos
        self.scheduler.add(agent)
        if pos is not None:
            self.grid.place_agent(agent, pos)

    def num_agents(self):
        """Returns total number of agents in the models."""
        return len(self.schedule.agents)

    def _branch(self):
        """TODO"""
        self.snapshot_branch = SnapshotBranch(parent_branch_id=self.snapshot_branch.id)

    def snapshot(self, commit=True):
        """TODO

        TODO

        Args:
            commit: TODO

        Returns:
          TODO
        """
        if self.snapshot_branch is None:
            self.snapshot_branch = SnapshotBranch()
        else:
            if self.snapshot_branch.version_id is not None:
                self.snapshot_branch.version_id += 1
        try:
            last_saved_branch_state = AgentModelState.query \
                .join(AgentModelSnapshot) \
                .join(SnapshotBranch, SnapshotBranch.id == self.snapshot_branch.id) \
                .order_by(AgentModelState.step_num.desc()) \
                .limit(1) \
                .first()
            if (last_saved_branch_state is not None and
                    last_saved_branch_state.step_num >= self.step_num):
                self._branch()
            agent_model_state = AgentModelState(step_num=self.step_num,
                                                grid_width=self.grid.width,
                                                grid_height=self.grid.height,
                                                model_time=self.time,
                                                seed=self.seed,
                                                random_state=self.random_state,
                                                minutes_per_step=self.minutes_per_step,
                                                termination=json.dumps(self.termination),
                                                priorities=json.dumps(self.priorities),
                                                location=self.location,
                                                config=json.dumps(self.config))
            snapshot = AgentModelSnapshot(agent_model_state=agent_model_state,
                                          snapshot_branch=self.snapshot_branch)
            db.session.add(agent_model_state)
            db.session.add(snapshot)
            db.session.add(self.snapshot_branch)
            for agent in self.scheduler.agents:
                agent.snapshot(agent_model_state, commit=False)
            if commit:
                db.session.commit()
            return snapshot
        except StaleDataError:
            app.logger.warning("WARNING: StaleDataError during snapshot, probably a simultaneous"
                               "save, changing branch.")
            db.session.rollback()
            self._branch()
            return self.snapshot()

    def step(self):
        """TODO

        TODO
        """
        self.time += self.timedelta_per_step()
        self.daytime = int(self.time.total_seconds() / 60) % self.day_length_minutes
        for cond in self.termination:
            if cond['condition'] == "time":
                value = cond['value']
                unit = cond['unit']
                model_time = self.time.total_seconds()
                if unit == 'min':
                    model_time /= 60
                elif unit == 'hour':
                    model_time /= 3600
                elif unit == 'day':
                    model_time /= 3600 * self.day_length_hours
                elif unit == 'year':
                    model_time /= 3600 * self.day_length_hours * 365
                else:
                    model_time /= 3600 * self.day_length_hours
                if model_time > value:
                    self.is_terminated = True
                    self.termination_reason = 'time'
                    return
        for storage in self.get_agents_by_class(agent_class=StorageAgent):
            storage_id = '{}_{}'.format(storage.agent_type, storage.id)
            if storage_id not in self.storage_ratios:
                self.storage_ratios[storage_id] = {}
            temp, total = {}, None
            for attr in storage.attrs:
                if attr.startswith('char_capacity'):
                    currency = attr.split('_', 2)[2]
                    storage_unit = storage.attr_details[attr]['units']
                    storage_value = pq.Quantity(float(storage[currency]), storage_unit)
                    if not total:
                        total = storage_value
                    else:
                        storage_value.units = total.units
                        total += storage_value
                    temp[currency] = storage_value.magnitude.tolist()
            for currency in temp:
                if temp[currency] > 0:
                    self.storage_ratios[storage_id][currency + '_ratio'] = \
                        temp[currency] / total.magnitude.tolist()
                else:
                    self.storage_ratios[storage_id][currency + '_ratio'] = 0
        self.scheduler.step()
        app.logger.info("{0} step_num {1}".format(self, self.step_num))

    def timedelta_per_step(self):
        """TODO"""
        return datetime.timedelta(minutes=self.minutes_per_step)

    def remove(self, agent):
        """TODO"""
        self.scheduler.remove(agent)
        if hasattr(agent, "pos"):
            self.grid.remove_agent(agent)

    def get_agents_by_type(self, agent_type=None):
        """TODO

        TODO

        Args:
            agent_type: TODO

        Returns:
          TODO
        """
        if agent_type is None:
            return self.scheduler.agents
        else:
            return [agent for agent in self.scheduler.agents if agent.agent_type == agent_type]

    def get_agents_by_class(self, agent_class=None):
        """TODO

        TODO

        Args:
            agent_class: TODO

        Returns:
          TODO
        """
        if agent_class is None:
            return self.scheduler.agents
        else:
            return [agent for agent in self.scheduler.agents if isinstance(agent, agent_class)]

    def agent_by_id(self, id):
        """TODO

        TODO

        Args:
            id: TODO

        Returns:
          TODO
        """
        for agent in self.get_agents_by_type():
            if agent.id == id:
                return agent
        return None


class AgentModelInitializationParams(object):
    """TODO

    TODO

    Attributes:
          snapshot_branch: TODO
          seed: TODO
          random_state: TODO
          starting_step_num: TODO
          single_agent: TODO
          minutes_per_step: TODO
          termination: TODO
          priorities: TODO
          location: TODO
          config: TODO
    """
    snapshot_branch = None
    seed = None
    random_state = None
    starting_step_num = 0
    single_agent = 0
    minutes_per_step = 60
    termination = []
    priorities = []
    location = 'mars'
    config = {}

    def set_grid_width(self, grid_width):
        """TODO

        TODO

        Args:
            grid_width: TODO

        Returns:
          TODO
        """
        self.grid_width = grid_width
        return self

    def set_grid_height(self, grid_height):
        """TODO

        TODO

        Args:
            grid_height: TODO

        Returns:
          TODO
        """
        self.grid_height = grid_height
        return self

    def set_starting_step_num(self, starting_step_num):
        """TODO

        TODO

        Args:
            starting_step_num: TODO

        Returns:
          TODO
        """
        self.starting_step_num = starting_step_num
        return self

    def set_starting_model_time(self, starting_model_time):
        """TODO

        TODO

        Args:
            starting_model_time: TODO

        Returns:
          TODO
        """
        self.starting_model_time = starting_model_time
        return self

    def set_snapshot_branch(self, snapshot_branch):
        """TODO

        TODO

        Args:
            snapshot_branch: TODO

        Returns:
          TODO
        """
        self.snapshot_branch = snapshot_branch
        return self

    def set_seed(self, seed):
        """TODO

        TODO

        Args:
            seed: TODO

        Returns:
          TODO
        """
        self.seed = seed
        return self

    def set_random_state(self, random_state):
        """TODO

        TODO

        Args:
            random_state: TODO

        Returns:
          TODO
        """
        self.random_state = random_state
        return self

    def set_single_agent(self, single_agent):
        """TODO

        TODO

        Args:
            single_agent: TODO

        Returns:
          TODO
        """
        self.single_agent = single_agent
        return self

    def set_termination(self, termination):
        """TODO

        TODO

        Args:
            termination: TODO

        Returns:
          TODO
        """
        self.termination = termination
        return self

    def set_priorities(self, priorities):
        """TODO

        TODO

        Args:
            priorities: TODO

        Returns:
          TODO
        """
        self.priorities = priorities
        return self

    def set_minutes_per_step(self, minutes_per_step):
        """TODO

        TODO

        Args:
            minutes_per_step: TODO

        Returns:
          TODO
        """
        self.minutes_per_step = minutes_per_step
        return self

    def set_location(self, location):
        """TODO

        TODO

        Args:
            location: TODO

        Returns:
          TODO
        """
        self.location = location
        return self

    def set_config(self, config):
        """TODO

        TODO

        Args:
            config: TODO

        Returns:
          TODO
        """
        self.config = config
        return self


class AgentInitializerRecipe(metaclass=ABCMeta):
    """TODO"""

    @abstractmethod
    def init_agents(self, model):
        """TODO"""
        pass


class BaseLineAgentInitializerRecipe(AgentInitializerRecipe):
    """TODO

    TODO

    Attributes:
          AGENTS: TODO
          STORAGES: TODO
          SINGLE_AGENT: TODO
          AGENT_LIST: TODO
    """

    def __init__(self, config):
        """Creates an Agent Initializer object.

        TODO

        Args:
          config: Dict, TODO
        """
        self.AGENTS = config['agents']
        self.STORAGES = config['storages']
        self.SINGLE_AGENT = config['single_agent']

    def init_agents(self, model):
        """TODO

        TODO

        Args:
            model: TODO

        Returns:
          TODO
        """
        for type_name, instances in self.STORAGES.items():
            for instance in instances:
                model.add_agent(StorageAgent(model=model,
                                             agent_type=type_name,
                                             **instance))
        for type_name, instances in self.AGENTS.items():
            for instance in instances:
                connections, amount = instance["connections"], instance['amount']
                if self.SINGLE_AGENT == 1:
                    model.add_agent(GeneralAgent(model=model,
                                                 agent_type=type_name,
                                                 connections=connections,
                                                 amount=amount))
                else:
                    for i in range(amount):
                        model.add_agent(GeneralAgent(model=model,
                                                     agent_type=type_name,
                                                     connections=connections,
                                                     amount=1))
        return model
