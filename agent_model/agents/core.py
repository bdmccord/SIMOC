r"""Describes Core Agent Types.
"""

import math
import random
import operator
from abc import ABCMeta
from time import time

import numpy as np
import quantities as pq
from mesa import Agent

from agent_model.attribute_meta import AttributeHolder
from agent_model.agents import growth_func, variation_func
from agent_model.agents import custom_funcs
from agent_model.exceptions import AgentInitializationError

class BaseAgent(Agent, AttributeHolder, metaclass=ABCMeta):
    """Initializes and manages refs, metadata, currency_dict, and AttributeHolder"""

    def __init__(self, *args, **kwargs):
        """Sets refs and metadata, initializes currency_dict and AttributeHolder

        Args:
          model:                AgentModel  mesa model which agent is added to
          agent_type:           str         e.g. 'human_agent'
          unique_id:            int
          agent_desc:           dict        from AgentModelInitializer
          active:               bool
        """
        self.model = kwargs.pop("model", None)
        self.agent_type = kwargs.pop("agent_type", None)
        self.unique_id = kwargs.pop("unique_id", random.getrandbits(63))
        self.active = kwargs.pop("active", True)
        self.amount = kwargs.pop('amount', 1)

        agent_desc = kwargs.pop("agent_desc", None)
        self.agent_class = agent_desc['agent_class']
        self.agent_type_id = agent_desc['agent_type_id']
        self.attrs = agent_desc['attributes']
        self.attr_details = agent_desc['attribute_details']

        init_type = kwargs.get("init_type")
        if init_type == "from_model":
            self.initial_variable = kwargs.pop("initial_variable")
            self.step_variation = kwargs.pop("step_variation")
            self.step_variable = kwargs.pop("step_variable")
        else:
            global_entropy = self.model.global_entropy
            if global_entropy == 0 or 'variation' not in agent_desc:
                self.initial_variable = 1
                self.step_variation = None
                self.step_variable = 1
            else:
                self._init_variation(agent_desc['variation'])

        self.currency_dict = {}
        AttributeHolder.__init__(self)
        super().__init__(self.unique_id, self.model)

    def _init_variation(self, variation):
        ge = self.model.global_entropy
        iv = variation.get('initial')
        sv = variation.get('step')
        if iv:
            upper = iv.get('upper', 0)
            lower = iv.get('lower', 0)
            distribution = iv.get('distribution')
            stdev_range = iv.get('stdev_range', None)
            characteristics = iv.get('characteristics', [])
            if isinstance(upper, dict) or isinstance(lower, dict):
                # When currency values are specified individually
                self.initial_variable = variation_func.get_variable(
                    self.model.random_state, ge, ge, distribution, stdev_range)
                if self.initial_variable == 1:
                    return
                elif self.initial_variable < 1:
                    y_ref = lower
                elif self.initial_variable > 1:
                    y_ref = upper
                x_norm = abs(self.initial_variable - 1)
                for attr, attr_value in self.attrs.items():
                    prefix, field = attr.split('_', 1)
                    if prefix in {'in', 'out'} or field in characteristics:
                        if field not in y_ref:
                            raise ValueError(f"Missing variation value for {self.agent_type} {field}.")
                        self.attrs[attr] = np.interp(x_norm, [0, 1], [attr_value, y_ref[field]])
            else:
                # When a scalar is used
                upper = upper * ge
                lower = lower * ge
                self.initial_variable = variation_func.get_variable(
                    self.model.random_state, upper, lower, distribution, stdev_range)
                for attr, attr_value in self.attrs.items():
                    prefix, field = attr.split('_', 1)
                    if prefix in ['in', 'out'] or field in characteristics:
                        self.attrs[attr] = attr_value * self.initial_variable
        if sv:
            upper = ge * sv.get('upper', 0)
            lower = ge * sv.get('lower', 0)
            distribution = sv.get('distribution')
            self.step_variation = dict(upper=upper, lower=lower, distribution=distribution)
            self.step_variable = 1

    def generate_step_variable(self):
        return variation_func.get_variable(self.model.random_state, **self.step_variation)

    def add_currency_to_dict(self, currency):
        """Adds a reference to the currency's database object.

        Args:
          currency: str, match currency naming convention
        """
        if currency not in self.currency_dict:
            currency_data = self.model.currency_dict[currency]
            self.currency_dict[currency] = currency_data
            if currency_data['type'] == 'currency':
                self.add_currency_to_dict(currency_data['class'])

    def destroy(self, reason):
        """Destroys the agent and removes it from the model"""
        self.cause_of_death = reason
        self.active = False
        self.model.remove(self)


class StorageAgent(BaseAgent):
    """Initialize and manage storage capacity """

    def __init__(self, *args, **kwargs):
        """Set initial currency balances; create new attributes for class capacities

        Args:
          id:           int     storage-specific id
          ...[currency] int     starting balance, from config
          ...[attributes & attribute_details inherited from BaseAgent]
        """
        super().__init__(*args, **kwargs)
        self.id = kwargs.get("id", None)
        self.has_storage = False
        class_capacities = {}
        class_unit = {}
        for attr, attr_value in self.attrs.items():
            if attr.startswith('char_capacity'):
                if not self.has_storage:
                    self.has_storage = True
                self._attr(attr, attr_value)
                currency = attr.split('_', 2)[2]
                self._attr(currency, kwargs.get(currency, 0))
                # Add meta-attributes for currency classes, so that inputs/outputs
                # can use the same mechanisms to reference them.
                self.add_currency_to_dict(currency)
                currency_class = self.currency_dict[currency]['class']
                if currency_class not in class_capacities:
                    class_capacities[currency_class] = 0
                    class_unit[currency_class] = self.attr_details[attr]['unit']
                class_capacities[currency_class] += attr_value
        for currency_class, capacity in class_capacities.items():
            class_attr = 'char_capacity_' + currency_class
            if class_attr not in self:
                self._attr(class_attr, capacity)
                self.attr_details[class_attr] = dict(unit=class_unit[currency_class])
        self._calculate_storage_ratios()

    def step(self):
        """Calculate storage ratios"""
        # TODO: This should be moved to self.increment() and streamlined
        if self.has_storage:
            self._calculate_storage_ratios()

    def _calculate_storage_ratios(self):
        storage_id = self.agent_type
        if storage_id not in self.model.storage_ratios:
            self.model.storage_ratios[storage_id] = {}
        temp, total = {}, None
        for attr in self.attrs:
            if attr.startswith('char_capacity'):
                currency = attr.split('_', 2)[2]
                storage_unit = self.attr_details[attr]['unit']
                storage_value = pq.Quantity(float(self[currency]), storage_unit)
                if not total:
                    total = storage_value
                else:
                    storage_value.units = total.units
                    total += storage_value
                temp[currency] = storage_value.magnitude.tolist()
        for currency in temp:
            if temp[currency] > 0:
                self.model.storage_ratios[storage_id][currency + '_ratio'] = \
                    temp[currency] / total.magnitude.tolist()
            else:
                self.model.storage_ratios[storage_id][currency + '_ratio'] = 0

    def view(self, view=None):
        if view not in self.currency_dict:
            raise KeyError(f"{view} is not a recognized view.")
        currency_data = self.currency_dict[view]
        if currency_data['type'] == 'currency':
            currency = view
            return {currency: self[currency]}
        elif currency_data['type'] == 'currency_class':
            currencies = currency_data['currencies']
            return {c: self[c] for c in currencies if c in self}
        else:
            raise KeyError(f"Currency {currency_data['name']} type not recognized by view.")

    def increment(self, view, increment_amount):
        """Increase or decrease storage balances

        If increment amount is positive, the view must be a currency and amount
        is limited by storage capacity.

        If increment is negative, view may be currency or currency_class. If
        currency_class, split amount between available currencies of class in
        proportion to their current balance.

        Return a dict with currency:value pairs of the actual amount
        incremented.

        """
        if increment_amount > 0:
            currency = view
            if self.currency_dict[currency]['type'] != 'currency':
                raise ValueError(f"Positive increment can only be used with currencies.")
            capacity = self['char_capacity_' + currency] * self.amount
            currency_amount = min(self[currency] + increment_amount, capacity)
            currency_increment_actual = self[currency] - currency_amount
            self[currency] = currency_amount
            return {currency: currency_increment_actual}
        elif increment_amount < 0:
            currencies = self.view(view)
            total_view_amount = sum(currencies.values())
            target_view_amount = min(total_view_amount + increment_amount, 0)
            # TODO: This is triggered sometimes by a rounding error. Need a better solution.
            # if target_view_amount < 0:
                # raise ValueError(f"{self.agent_type} has insufficient {view} balance to increment by {increment_amount}")
            if total_view_amount <= 0:
                return {c: 0 for c in currencies}
            ratios = {c: self[c]/total_view_amount for c in currencies.keys()}
            flow = {}
            for currency in currencies:
                currency_increment_target = increment_amount * ratios[currency]
                currency_amount = max(self[currency] + currency_increment_target, 0)
                currency_increment_actual = self[currency] - currency_amount
                self[currency] = currency_amount
                flow[currency] = currency_increment_actual
            return flow
        else:
            return {}


class GeneralAgent(StorageAgent):
    """The base class for a SIMOC agent.

    Stores and manages a stateful representation of a single SIMOC agent;
    manages storage and currency exchanges.

    ====================== ============== ===============
          Attribute        Type               Description
    ====================== ============== ===============
    ``agent_type``         str            Agent name, e.g. 'rice'
    ``agent_type_id``      int            Randomly-generated id for agent_type
    ``unique_id``          int            Randomly-generated id for instance
    ``agent_class``        str            Agent class, e.g. 'plants'
    ``active``             bool           Whether step function is called
    ``attrs``              dict           A dict containing key:value pairs or currency exchanges and characteriscits; see :ref:`agent-desc`
    ``attr_details``       dict           Extra information about attrs, e.g. 'unit'
    ``currency_dict``      dict           A subset of the currency_dict in :ref:`agent-model` with only currencies used by this agent
    ``id``                 int            Index for storage type (not used)
    ``has_storage``        bool           Whether agent has storage characteristics
    ``...[currency]``      float          Current storage balance of a currency
    ``has_flows``          bool           Whether agent has currency exchanges
    ``connections``        dict           A list of all connected agents
    ``selected_storage``   dict           Lists of connected agents sorted by direction, currency
    ``buffer``             dict           Current buffer size (i.e. steps before activate/deactivate) per currency
    ``deprive``            dict           Current deprive available (i.e. steps before death) per currency
    ``step_values``        dict           Step values for each currency with lifetime/daily growth applied
    ``events``             dict           A list of event instances by type
    ``event_multipliers``  dict           A list of multipliers from events, applied to every currency exchange
    ====================== ============== ===============
    """

    def __init__(self, *args, **kwargs):
        """Initialize currency exchange fields and copy relevant data"""
        super().__init__(*args, **kwargs)
        self.age = kwargs.pop("age", 0)
        self.has_flows = False
        self.connections = kwargs.get("connections", {})
        self.selected_storage = {"in": {}, 'out': {}}
        self.buffer = kwargs.pop("buffer", {})
        self.deprive = kwargs.pop("deprive", {})
        self.step_values = kwargs.get("step_values", {})
        self.events = kwargs.get("events", {})
        self.event_multipliers = kwargs.get("event_multipliers", {})

        if kwargs.get('init_type') == 'from_new' and self.model.global_entropy != 0:
            self.process_events = True
            self._init_events()
        else:
            self.process_events = False

    def _init_events(self):
        for attr in self.attrs:
            if not attr.startswith('event'):
                continue
            # Normalize unit values
            probability = self.attr_details[attr]['probability_value']
            probability_unit = self.attr_details[attr]['probability_unit']
            multiplier = dict(min=60, hour=1, day=1/int(self.model.day_length_hours)).get(probability_unit, 0)
            self.attr_details[attr]['probability_per_step'] = probability * self.model.hours_per_step * multiplier

            duration = self.attr_details[attr].get('duration_value', None)
            if duration:
                duration_unit = self.attr_details[attr].get('duration_unit', 'hour')
                multiplier = dict(min=60, hour=1, day=1/int(self.model.day_length_hours)).get(duration_unit, 1)
                self.attr_details[attr]['duration_delta_per_step'] = self.model.hours_per_step * multiplier


    def _init_currency_exchange(self, n_steps=None):
        """Initialize all values related to currency exchanges

        This includes making connections to other live Agents, so it must be
        isolated from __init__ and called after all Agents are initialized.
        """
        # For PlantAgents, n_steps is calculated in PlantAgent.__init__ and passed
        n_steps = n_steps or int(self.model.day_length_hours)
        hours_per_step = self.model.hours_per_step
        day_length_hours = self.model.day_length_hours
        for attr in self.attrs:
            prefix, currency = attr.split('_', 1)
            if prefix not in ['in', 'out']:
                continue
            if not self.has_flows:
                self.has_flows = True
            self.add_currency_to_dict(currency)
            attr_details = self.attr_details[attr]
            attr_unit = attr_details['flow_unit']

            # Connections
            connected_agents = self.connections[prefix][currency]
            if len(connected_agents) == 0:
                raise AgentInitializationError(
                    f"No connection found for {currency} in {self.agent_type}.")
            self.selected_storage[prefix][currency] = []
            for agent_type in connected_agents:
                if agent_type == self.agent_type:
                    storage_agent = self
                else:
                    storage_agent = self.model.get_agents_by_type(agent_type=agent_type)[0]
                storage_unit = storage_agent.attr_details['char_capacity_' + currency]['unit']
                if storage_unit != attr_unit:
                    raise AgentInitializationError(
                        f"Units for {self.agent_type} {currency} ({attr_unit}) "
                        f"do not match storage ({storage_unit})")
                self.selected_storage[prefix][currency].append(storage_agent)

            # Deprive
            deprive_value = attr_details.get('deprive_value', None)
            if deprive_value and attr not in self.deprive:
                self.deprive[attr] = deprive_value * self.amount
                deprive_unit = attr_details['deprive_unit']
                multiplier = dict(min=60, hour=1, day=1/int(self.model.day_length_hours)).get(deprive_unit, 0)
                self.attr_details[attr]['delta_per_step'] = self.model.hours_per_step * multiplier

            # Criteria Buffer
            cr_buffer = attr_details.get('criteria_buffer', None)
            if cr_buffer and attr not in self.buffer:
                    self.buffer[attr] = cr_buffer

            # Step Values
            if attr not in self.step_values:
                step_values = self._calculate_step_values(attr, n_steps, hours_per_step, day_length_hours)
                self.step_values[attr] = step_values

    def _calculate_step_values(self, attr, n_steps, hours_per_step, day_length_hours):
        """Calculate lifetime step values based on growth functions and add to self.step_values

        """
        step_values = []
        ad = self.attr_details[attr]
        agent_flow_time = ad['flow_time']
        lifetime_growth_type = ad['lifetime_growth_type']
        lifetime_growth_center = ad['lifetime_growth_center']
        lifetime_growth_min_value = ad['lifetime_growth_min_value']
        lifetime_growth_max_value = ad['lifetime_growth_max_value']
        daily_growth_type = ad['daily_growth_type']
        daily_growth_center = ad['daily_growth_center']
        daily_growth_min_value = ad['daily_growth_min_value']
        lifetime_growth_min_threshold = ad['lifetime_growth_min_threshold']
        lifetime_growth_max_threshold = ad['lifetime_growth_max_threshold']
        daily_growth_min_threshold = ad['daily_growth_min_threshold']
        daily_growth_max_threshold = ad['daily_growth_max_threshold']
        daily_growth_invert = ad['daily_growth_invert']
        lifetime_growth_invert = ad['lifetime_growth_invert']
        daily_growth_noise = ad['daily_growth_noise']
        lifetime_growth_noise = ad['lifetime_growth_noise']
        daily_growth_scale = ad['daily_growth_scale']
        lifetime_growth_scale = ad['lifetime_growth_scale']
        daily_growth_steepness = ad['daily_growth_steepness']
        lifetime_growth_steepness = ad['lifetime_growth_steepness']

        multiplier = 1
        if agent_flow_time == 'min':
            multiplier *= (hours_per_step * 60)
        elif agent_flow_time == 'hour':
            multiplier *= hours_per_step
        elif agent_flow_time == 'day':
            multiplier *= hours_per_step / day_length_hours
        else:
            raise Exception('Unknown agent flow_rate.time value.')
        agent_value = float(self.attrs[attr])
        agent_value *= float(multiplier)

        if lifetime_growth_type:
            start_value = lifetime_growth_min_value or 0.0
            max_value = lifetime_growth_max_value or 0.0
            center = lifetime_growth_center or None
            min_threshold = lifetime_growth_min_threshold or 0.0
            min_threshold *= n_steps
            max_threshold = lifetime_growth_max_threshold or 0.0
            max_threshold *= n_steps
            invert = bool(lifetime_growth_invert)
            noise = bool(lifetime_growth_noise)
            kwargs = {'agent_value': agent_value,
                        'max_value': max_value,
                        'num_values': n_steps,
                        'growth_type': lifetime_growth_type,
                        'min_value': start_value,
                        'min_threshold': int(min_threshold),
                        'max_threshold': int(max_threshold),
                        'center': center,
                        'noise': noise,
                        'invert': invert}
            if lifetime_growth_scale:
                kwargs['scale'] = lifetime_growth_scale
            if lifetime_growth_steepness:
                kwargs['steepness'] = lifetime_growth_steepness
            step_values = growth_func.get_growth_values(**kwargs)
        else:
            step_values = np.ones(n_steps) * agent_value

        if daily_growth_type:
            day_length = int(day_length_hours)
            center = daily_growth_center or None
            min_threshold = daily_growth_min_threshold or 0.0
            min_threshold *= day_length_hours
            max_threshold = daily_growth_max_threshold or 0.0
            max_threshold *= day_length_hours
            invert = bool(daily_growth_invert)
            noise = bool(daily_growth_noise)
            for i in range(0, n_steps, day_length):
                day_values = step_values[i:i+day_length]
                agent_value = np.mean(day_values)
                daily_min = np.min(day_values)
                daily_max = np.max(day_values)
                if daily_growth_min_value:
                    start_value = agent_value * daily_growth_min_value
                elif daily_min < daily_max:
                    start_value = daily_min or 0
                else:
                    start_value = 0
                if (i + day_length) > n_steps:
                    day_length = n_steps - i
                kwargs = {'agent_value': agent_value,
                          'num_values': day_length,
                          'growth_type': daily_growth_type,
                          'min_value': start_value,
                          'min_threshold': int(min_threshold),
                          'max_threshold': int(max_threshold),
                          'center': center,
                          'noise': noise,
                          'invert': invert}
                if daily_growth_scale:
                    kwargs['scale'] = daily_growth_scale
                elif lifetime_growth_type:
                    kwargs['scale'] = 0.5
                    kwargs['max_value'] = agent_value * 1.1
                if daily_growth_steepness:
                    kwargs['steepness'] = daily_growth_steepness
                if start_value == agent_value:
                    step_values[i:i+day_length] = np.ones(day_length) * agent_value
                else:
                    step_values[i:i+day_length] = growth_func.get_growth_values(**kwargs)
        return step_values

    def _get_storage_ratio(self, cr_name):
        total = 0
        direction = cr_name.split('_')[-1]
        storage_ratios = self.model.storage_ratios
        if direction in ['in', 'out']:
            #  e.g. 'co2_ratio_in': Return value for specific connection
            elements = cr_name.split('_')
            currency = elements[0]
            cr_actual = '_'.join(elements[:2])
            storage_id = self.selected_storage[direction][currency][0].agent_type
            total += storage_ratios[storage_id][cr_actual]
        else:
            # e.g. 'co2_ratio': Return value for all storages with currency
            # TODO: not used?
            for storage_id in storage_ratios:
                if cr_name in storage_ratios[storage_id]:
                    total += storage_ratios[storage_id][cr_name]
        return total

    def _get_step_value(self, attr, step_num):
        """TODO

        TODO

        Args:
            attr: TODO

        Returns:
          TODO
        """
        prefix, currency = attr.split('_', 1)
        agent_unit = self.attr_details[attr]['flow_unit']
        cr_name = self.attr_details[attr]['criteria_name']
        if cr_name:
            cr_limit = self.attr_details[attr]['criteria_limit']
            cr_value = self.attr_details[attr]['criteria_value'] or 0.0
            cr_buffer = self.attr_details[attr]['criteria_buffer'] or 0.0
            if cr_name in self:
                # e.g. 'growth_rate'
                source = self[cr_name]
            else:
                source = self._get_storage_ratio(cr_name)
            opp = {'>': operator.gt, '<': operator.lt, '=': operator.eq}[cr_limit]
            if opp(source, cr_value):
                if cr_buffer > 0 and self.buffer.get(attr, 0) > 0:
                    self.buffer[attr] -= 1
                    return pq.Quantity(0.0, agent_unit)
            else:
                if cr_buffer > 0:
                    self.buffer[attr] = cr_buffer
                return pq.Quantity(0.0, agent_unit)
        if step_num >= self.step_values[attr].shape[0]:
            step_num = step_num % int(self.model.day_length_hours)
        agent_value = self.step_values[attr][step_num]
        return pq.Quantity(agent_value, agent_unit)

    def _process_event(self, attr, attr_value):
        event_type = attr.split('_', 1)[1]
        attr_details = self.attr_details[attr]
        # UPDATE INSTANCES FOR DURATION & AMOUNT
        instances = self.events.get(event_type, [])
        for instance in instances:
            if 'duration' in instance:
                instance['duration'] -= attr_details['duration_delta_per_step']
        instances = list(filter(
            lambda e: False if 'duration' in e and e['duration'] <= 0 else True, instances))
        if len(instances) > self.amount:
            instances = instances[:self.amount]
        # RANDOMLY GENERATE NEW INSTANCES
        scope = attr_details['scope']
        max_instances = 1 if scope == 'group' else self.amount
        max_new_instances = max_instances - len(instances)
        for i in range(max_new_instances):
            # Roll the dice
            instance_variable = self.model.random_state.rand()
            if instance_variable > attr_details['probability_per_step']:
                continue
            # Add instance
            if attr_value == 'termination':
                self.kill(f"Agent died due to {event_type}")
            elif attr_value == 'multiplier':
                magnitude = attr_details['magnitude_value']
                magnitude_variation_distribution = attr_details.get('magnitude_variation_distribution')
                if magnitude_variation_distribution:
                    magnitude_variable = variation_func.get_variable(
                        self.model.random_state,
                        attr_details['magnitude_variation_upper'],
                        attr_details['magnitude_variation_lower'],
                        magnitude_variation_distribution
                    )
                    magnitude = magnitude * magnitude_variable
                instance = dict(magnitude=magnitude)
                duration = attr_details.get('duration_value')
                if duration:
                    duration_variation_distribution = attr_details.get('duration_variation_distribution')
                    if duration_variation_distribution:
                        duration_variable = variation_func.get_variable(
                            self.model.random_state,
                            attr_details['duration_variation_upper'],
                            attr_details['duration_variation_lower'],
                            duration_variation_distribution
                        )
                        duration = duration * duration_variable
                    instance['duration'] = duration
                instances.append(instance)
        # UPDATE EVENT RECORDS
        if len(instances) == 0 and event_type in self.events:
            del self.events[event_type]
            del self.event_multipliers[event_type]
        elif len(instances) > 0:
            self.events[event_type] = instances
            modified = sum([i['magnitude'] for i in instances])
            unmodified = max_instances - len(instances)
            event_multiplier = (modified + unmodified) / max_instances
            self.event_multipliers[event_type] = event_multiplier


    def step(self, value_eps=1e-12, value_round=6):
        """The main step function for SIMOC agents. Calculate step values and process exchanges.

        """
        super().step()

        # Log actual exchanges from each step. Reset at the beginning of each
        # step, and used afterwards by DataCollector to log actual values.
        # If no value is found in the step_exchange_buffer, DataCollector logs
        # a 0.
        self.step_exchange_buffer = {'in': {}, 'out': {}}

        self.age += self.model.hours_per_step
        for attr, attr_value in self.attrs.items():
            # 1. CHECK THRESHOLDS
            if attr.startswith('char_threshold_'):
                (threshold_type, currency) = attr.split('_')[-2:]
                for prefix in ['in', 'out']:
                    if currency in self.selected_storage[prefix]:
                        for storage_agent in self.selected_storage[prefix][currency]:
                            agent_id = storage_agent.agent_type
                            storage_ratio = self.model.storage_ratios[agent_id][currency + '_ratio']
                            opp = {'upper': operator.gt, 'lower': operator.lt}[threshold_type]
                            if opp(storage_ratio, attr_value):
                                self.kill('Threshold {} met for {}. Killing the agent'.format(
                                    currency, self.agent_type))
                                return
            # 2. EXECUTE CUSTOM FUNCTIONS
            if attr == 'char_custom_function':
                custom_function = getattr(custom_funcs, attr_value)
                if custom_function:
                    custom_function(self)
                else:
                    raise Exception(f'Unknown custom function: {custom_function}.')
            # 3. PROCESS EVENTS
            if attr.startswith('event') and self.process_events:
                self._process_event(attr, attr_value)

        # 4. GENERATE RANDOM VARIATION
        if self.step_variation is not None:
            self.step_variable = self.generate_step_variable()

        # ITERATE THROUGH EACH INPUT AND OUTPUT
        influx = set()     # For 'requires' field
        self.missing_desired = False  # Stalls growth if 'required = desired' field is missing
        for prefix in ['in', 'out']:
            for currency, selected_storages in self.selected_storage[prefix].items():
                attr = f"{prefix}_{currency}"
                attr_details = self.attr_details[attr]

                # 5. CHECK ESCAPE PARAMETERS
                if self.attrs[attr] == 0:
                    # e.g. Atmosphere Equalizer: uses custom_func, has dummy flow to initialize connections
                    continue
                requires = attr_details.get('requires') or []
                if len(requires) > 0 and len(set(requires).difference(influx)) > 0:
                    # e.g. Human: if not consume potable water, don't produce urine
                    continue

                # 6. CALCULATE TARGET VALUE
                step_num = int(self.age)
                step_value = self._get_step_value(attr, step_num)     # type pq.Quantity
                step_value = step_value * self.step_variable
                step_value = step_value * np.prod(list(self.event_multipliers.values()))
                step_mag = step_value.magnitude.tolist()    # type float
                target_value = step_mag * self.amount
                actual_value = target_value                 # to be adjusted below

                # 7. CALCULATE AVAILABLE VALUE
                available_value = 0   # Total available in connected storages
                available_conns = []  # Value/capacity for each storage
                for storage in selected_storages:
                    storage_value = sum(storage.view(currency).values())
                    storage_cap = storage['char_capacity_' + currency]
                    storage_amount = storage.__dict__.get('amount', 1)
                    available_value += storage_value
                    available_conns.append(dict(
                        agent=storage,
                        value=storage_value,
                        capacity=storage_cap * storage_amount
                    ))

                # 8. UPDATE AGENT BASED ON DEFICIT/SUFFICIENCY
                has_deficit = prefix == 'in' and available_value < target_value
                # 8.1 REQUIRES
                is_required = attr_details.get('is_required')
                if has_deficit and is_required:
                    if is_required == 'mandatory':
                        # e.g. Dehumidifier: If there's no atmosphere.h2o, don't do anything.
                        return
                    elif is_required == 'desired':
                        # e.g. Plants: If one or more desired inputs is missing, growth stalls.
                        self.missing_desired = True
                # 8.2 DEPRIVE
                deprive_value = attr_details.get('deprive_value') or 0
                if has_deficit and deprive_value > 0:
                    n_satisfied = math.floor(available_value / step_mag)
                    actual_value = n_satisfied * step_mag
                    delta_per_step = attr_details.get('delta_per_step', 0)
                    max_survive = math.floor(max(self.deprive[attr], 0) / delta_per_step)
                    n_deprived = self.amount - n_satisfied
                    n_survive = min(n_deprived, max_survive)
                    self.deprive[attr] -= delta_per_step * n_survive
                    n_die = n_deprived - n_survive
                    self.amount -= n_die
                    if self.amount <= 0:
                        self.kill(f'All {self.agent_type} died from lack of {currency}. Killing the agent')
                        return
                elif deprive_value > 0:
                    self.deprive[attr] = min(deprive_value * self.amount,
                                             self.deprive[attr] + deprive_value)

                # 9. PROCESS EXCHANGE
                remaining_value = actual_value
                for conn in available_conns:
                    storage = conn['agent']
                    if prefix == 'in':
                        conn_delta = min(remaining_value, conn['value'])
                        flow = storage.increment(currency, -conn_delta)
                    elif prefix == 'out':
                        conn_delta = remaining_value / len(available_conns)
                        flow = storage.increment(currency, conn_delta)
                    remaining_value -= conn_delta
                    if actual_value < value_eps:  # ignore values less than 1e-12
                        continue
                    # Log exchanged values
                    if prefix == 'in':
                        influx.add(currency)
                    buf = self.step_exchange_buffer[prefix]
                    for _currency, _amount in flow.items():
                        if _currency not in buf:
                            buf[_currency] = {}
                        buf[_currency][storage.agent_type] = abs(_amount)


    def kill(self, reason):
        """Destroy the agent and remove it from the model

        Args:
          reason: str, cause of death
        """
        self.destroy(reason)


class PlantAgent(GeneralAgent):
    """Initialize and manage growth, amount and reproduction

    ====================== ============== ===============
          Attribute        Type               Description
    ====================== ============== ===============
    ``full_amount``        str            Maximum/reset amount as defined in agent_desc
    ``lifetime``           int            Hours to complete growth cycle.
    ``reproduce``          bool
    ``growth_criteria``    str            Which currency/attribute determines growth
    ``total_growth``       int            The sum of all step values for growth_criteria currency
    ``delay_start``        int            Hours to wait before starting growth
    ``agent_step_num``     int            Current step in growth cycle, as limited by growth_critera
    ``currenct_growth``    int            Accumulated values for growth_criteria item
    ``growth_rate``        int            Accumulated % for growth_criteria item
    ``grown``              bool           Whether growth is complete
    ====================== ============== ===============
    """

    def __init__(self, *args, delay_start=0, full_amount=None, agent_step_num=0,
                 total_growth=0, current_growth=0, growth_rate=0, grown=False,
                 **kwargs):
        """Set the age and amount, parse attributes and intialize growth-tracking fields"""
        super().__init__(*args, **kwargs)
        self.delay_start = delay_start
        self.full_amount = full_amount or self.amount
        self.agent_step_num = agent_step_num
        self.total_growth = total_growth
        self.current_growth = current_growth
        self.growth_rate = growth_rate
        self.grown = grown

        if 'char_lifetime' in self.attrs:
            lifetime = self.attrs['char_lifetime']
            lifetime_unit = self.attr_details['char_lifetime']['unit']
            lifetime_multiplier = dict(day=self.model.day_length_hours, hour=1, min=1/60)[lifetime_unit]
            self.lifetime = lifetime * int(lifetime_multiplier)
        else:
            self.lifetime = 0
        self.reproduce = self.attrs.get('char_reproduce', 0)
        self.growth_criteria = self.attrs.get('char_growth_criteria', None)
        self.carbon_fixation = self.attrs.get('char_carbon_fixation', None)


    def _init_currency_exchange(self):
        n_steps = int(self.lifetime)
        super()._init_currency_exchange(n_steps)
        if self.growth_criteria and self.total_growth == 0:
            self.total_growth = float(np.sum(self.step_values[self.growth_criteria]))
        self.co2_scale = {}
        for attr in self.attrs:
            prefix, _ = attr.split('_', 1)
            if prefix in ['in', 'out']:
                self.co2_scale[attr] = 1


    def _get_step_value(self, attr, step_num):
        # On last step in lifecycle, only return the `weighted` value, i.e. the
        # food output, and ignore any criteria.
        cr_name = self.attr_details[attr]['criteria_name']
        if self.grown and cr_name != 'grown':
            agent_unit = self.attr_details[attr]['flow_unit']
            return pq.Quantity(0.0, agent_unit)

        # Step values for plants are based on `agent_step_num`` instead of `age`
        # to account for stalled growth from deprive. TODO: As noted above,
        # `agent_step_num` and other growth-related variables *should* be
        # updated here, but the current reporting system expects `growth` in
        # the main step record, so it's included in GeneralAgent.step().
        step_num = int(self.agent_step_num)
        step_value = super()._get_step_value(attr, step_num)

        weighted = self.attr_details[attr]['weighted']
        if weighted and weighted in self:
            step_value *= self[weighted]
        if attr in self.co2_scale:
            step_value *= self.co2_scale[attr]
        return step_value


    def _calculate_co2_scale(self, step_num, value_eps=1e-12):
        """Calculate a multiplier for each currency exchange based on ambient co2"""

        # Get/Initialize cache
        co2 = self.model.storage_ratios.get('co2', None)
        if not co2:
            co2 = dict(time=0, co2_uptake_ratio=1, transpiration_efficiency_factor=1)
            self.model.storage_ratios['co2'] = co2

        # Check for cached value at time, else calculate
        if co2['time'] == str(self.model.time):
            co2_uptake_ratio = co2['co2_uptake_ratio']
            transpiration_efficiency_factor = co2['transpiration_efficiency_factor']
        else:
            co2_ppm = self._get_storage_ratio('co2_ratio_in') * 1e6
            co2_ppm = max(350, min(1000, co2_ppm)) # Limit effect to 350-1000ppm range
            t_mean = 25 # Mean temperature for timestep. TODO: Link to connection

            # Calculate the ratio of increased co2 uptake [Vanuytrecht 5]
            if self.carbon_fixation == 'c3':
                tt = (163 - t_mean) / (5 - 0.1 * t_mean) # co2 compensation point
                numerator = (co2_ppm - tt) * (350 + 2 * tt)
                denominator = (co2_ppm + 2 * tt) * (350 - tt)
                co2_uptake_ratio = numerator/denominator
            else:
                co2_uptake_ratio = 1

            # Calculate the ratio of decreased water use [Vanuytrecht 7]
            co2_range = [350, 700]
            te_range = [1, 1.37]
            transpiration_efficiency_factor = np.interp(co2_ppm, co2_range, te_range)

            # Update cache
            self.model.storage_ratios['co2'] = dict(
                time=str(self.model.time),
                co2_uptake_ratio=co2_uptake_ratio,
                transpiration_efficiency_factor=transpiration_efficiency_factor)

        # Calculate co2-adjusted step values
        sv_baseline = {'in': {}, 'out': {}}
        sv_adjusted = {'in': {}, 'out': {}}
        cu_fields = ['co2', 'fertilizer', 'o2', 'biomass', self.agent_type]
        te_fields = ['potable', 'h2o']
        exclude = ['in_biomass', f'out_{self.agent_type}']
        for attr in self.attrs:
            prefix, currency = attr.split('_', 1)
            if prefix not in ['in', 'out']:
                continue
            step_value = self.step_values[attr][step_num]
            sv_baseline[prefix][currency] = step_value
            if attr in exclude:
                continue
            elif currency in cu_fields:
                sv_adjusted[prefix][currency] = step_value * co2_uptake_ratio
            elif currency in te_fields:
                sv_adjusted[prefix][currency] = step_value * (1 / transpiration_efficiency_factor)

        # Calculate error (difference between total inputs and outputs)
        net_inputs = sum(sv_adjusted['in'].values())
        net_outputs = sum(sv_adjusted['out'].values())
        sv_error = net_inputs - net_outputs

        # Remove error proportionally from outputs
        if sv_error > value_eps:
            corrected = {}
            for currency, value in sv_adjusted['out'].items():
                correction = (value / net_outputs) * sv_error
                corrected[currency] = value + correction
            sv_adjusted['out'] = corrected

        # Convert to scale factors (1 = no change)
        sv_scale = {}
        for prefix in ['in', 'out']:
            for currency, baseline in sv_baseline[prefix].items():
                attr_name = f"{prefix}_{currency}"
                adjusted = sv_adjusted[prefix].get(currency, None)
                sv_scale[attr_name] = 1 if not adjusted else adjusted / baseline
        return sv_scale


    def step(self):
        """TODO"""
        if self.delay_start > 0:
            self.delay_start -= self.model.hours_per_step
            return
        if self.grown:
            if self.reproduce:
                # Delay start until 'hour 0' so daylight cycles remain aligned
                # TODO: Any shortfall of currency delays aging and causes
                # disalignment; this will require a deeper redesign to fix.
                step_num = int(self.age)
                day_length = int(self.model.day_length_hours)
                current_hour = step_num % day_length
                delay = day_length - current_hour - 1

                self.age = 0
                self.current_growth = 0
                self.growth_rate = 0
                self.agent_step_num = 0
                self.grown = False
                self.amount = self.full_amount
                self.delay_start = delay
                return
            self.destroy(f'Lifetime limit has been reached by {self.agent_type}. Killing the agent.')

        if self.agent_step_num >= self.lifetime - 1 > 0:
            self.grown = True  # Complete last flow cycle and terminate next step
        elif self.carbon_fixation:
            # Calculate co2 multipliers for each currency for the next step
            step_num = int(self.agent_step_num + 1)
            self.co2_scale = self._calculate_co2_scale(step_num)

        super().step()

        if not self.missing_desired:
            # Advance life cycle
            self.agent_step_num += self.model.hours_per_step
            # Calculated updated growth
            growth_criteria = getattr(self, 'growth_criteria', None)
            prefix, currency = growth_criteria.split('_', 1)
            step_data = self.step_exchange_buffer[prefix].get(currency)
            if step_data:
                growth_value = sum(step_data.values())
                self.current_growth += (growth_value / self.amount)
                self.growth_rate = self.current_growth / self.total_growth
