"""
These functions perform calculations or assignments which
are to be passed back to the front end, but none of the functions
are called directly from the front end.
These functions were originally in views.py.
The front end routes which call the functions here are in views.py
Note: the name of this script is misleading and should be changed
"""

import os
import sys
import json
import math
import copy
import gzip
import pathlib
import datetime

import numpy as np
from flask import request, Response
from werkzeug.security import safe_join

from simoc_server import app, db, redis_conn
from simoc_abm.util import load_data_file, get_default_agent_data, get_default_currency_data
from simoc_abm.agents import SunAgent, ConcreteAgent

@app.route('/simdata/<path:filename>')
def serve_simdata(filename):
    """Serve static gzipped simdata files."""
    simdata_dir = pathlib.Path(__file__).resolve().parent / "dist" / "simdata"
    simdata_file = safe_join(simdata_dir, filename)  # prevent path traversal
    app.logger.info(f'Serving preset simdata file: {simdata_file}')
    try:
        with open(simdata_file, 'rb') as f:
            data = f.read()
    except FileNotFoundError:
        return "Invalid simdata file", 404
    else:
        return Response(data, mimetype='application/json',
                        headers={'Content-Encoding': 'gzip'})
    

@app.route('/get_energy', methods=['GET'])
def get_energy():
    """
    Sends front end energy values for config wizard.
    Takes in the request values 'agent_name' and 'quantity'

    Returns
    -------
    json object with energy value for agent
    """

    agent_name = request.args.get('agent_name', type=str)
    agent_quantity = request.args.get('quantity', 1, type=int) or 1
    agent_desc = load_data_file('agent_desc.json')
    data = agent_desc.get(agent_name, None)
    if agent_name == 'solar_pv_array_mars':
        value_type = 'energy_output'
        total_kwh = data['flows']['out']['kwh']['value']
    elif agent_name == 'power_storage':
        value_type = 'energy_capacity'
        total_kwh = data['capacity']['kwh']
    else:
        value_type = 'energy_input'
        def input_kwh(agent):
            return agent.get('flows', {}).get('in', {}).get('kwh', {}).get('value', 0)
        if agent_name == 'eclss':
            # Sum of all eclss agents
            total_kwh = sum(input_kwh(v) for v in agent_desc.values()
                            if v.get('agent_class') == 'eclss')
        elif data is None:
            total_kwh = 0
        elif data.get('agent_class', None) == 'plants':
            # Electricity used by lamp for plant
            par_baseline = data['properties']['par_baseline']['value']
            total_kwh = par_baseline * input_kwh(agent_desc['lamp'])
        else:
            total_kwh = input_kwh(data)
    value = total_kwh * agent_quantity
    total = {value_type : value}
    return json.dumps(total)

monthly_par = SunAgent.monthly_par
hourly_par_fraction = SunAgent.hourly_par_fraction
def b2_plant_factor(plant, data, cache={}):
    """Calculate and return an estimate of the actual:ideal exchange ratios at b2"""
    if plant not in cache:
        # par_factor
        mean_monthly_par = sum(monthly_par) / len(monthly_par)
        available_light = np.array(hourly_par_fraction) * mean_monthly_par
        par_baseline = data['properties']['par_baseline']['value']
        photoperiod = data['properties']['photoperiod']['value']
        photo_start = int((24 // 2) - (photoperiod // 2))
        photo_end = int(photo_start + photoperiod)
        photo_rate = 24 / photoperiod
        ideal_light = np.zeros(24)
        ideal_light[photo_start:photo_end] = photo_rate * par_baseline
        usable_light = np.minimum(available_light, ideal_light)
        par_factor_hourly = usable_light / ideal_light
        par_factor = np.mean(par_factor_hourly[photo_start:photo_end])
        # density_factor
        density_factor = 0.5
        cache[plant] = par_factor * density_factor
    return cache[plant]

carbonation_rate = (ConcreteAgent.rate_scale[0] * 
                    ConcreteAgent.density * 
                    ConcreteAgent.diffusion_rate)
@app.route('/get_o2_co2', methods=['GET'])
def get_o2_co2():
    """
    Sends front end o2 and co2 values for config wizard.
    Takes in the request values 'agent_name' and 'quantity'

    Returns
    -------
    json object with o2 and co2 values for agent
    """

    agent_name = request.args.get('agent_name', type=str)
    agent_quantity = request.args.get('quantity', 1, type=int) or 1
    location = request.args.get('location', 'mars', type=str)
    total = {'o2': {'input': 0, 'output': 0}, 'co2': {'input': 0, 'output': 0}}

    substitute_names = {'human_agent': 'human'}
    agent_name = substitute_names.get(agent_name, agent_name)
    data = get_default_agent_data(agent_name)
    if data is None:
        raise ValueError('Agent not found in agent_desc:', agent_name)
    agent_class = data.get('agent_class', None)
    if 'flows' not in data:
        return json.dumps(total)
    for direction, flows in data['flows'].items():  # input, output
        for currency in {'o2', 'co2'}:
            if currency not in flows:
                continue
            amount = flows[currency]['value'] * agent_quantity
            # for B2, adjust for expected model outputs
            if location == 'b2':
                if agent_class == 'plants':
                    amount *= b2_plant_factor(agent_name, data)
                elif agent_name == 'concrete':
                    amount *= carbonation_rate
            _direction = {'in': 'input', 'out': 'output'}[direction]
            total[currency][_direction] += amount
    return json.dumps(total)


def calc_air_storage(volume, weights=None):
    # 1 m3 of air weighs ~1.25 kg (depending on temperature and humidity)
    AIR_DENSITY = 1.25  # kg/m3
    # convert from m3 to kg
    mass = volume * AIR_DENSITY
    # atmosphere component breakdown (see PRD)
    weights = weights if weights is not None else {}
    percentages = {
        "n2": weights.get('n2', 78.084),  # nitrogen
        "o2": weights.get("o2", 20.946),  # oxygen
        "co2": weights.get("co2", 0.041332),  # carbon dioxide
        "ch4": weights.get("ch4", 0.000187),  # methane
        "h2": weights.get("h2", 0.000055),  # hydrogen
        "h2o": weights.get("h2o", 1),  # water vapor
        # the followings are not included
        #"atmo_ar": 0.9340,  # argon
        #"atmo_ne": 0.001818,  # neon
        #"atmo_he": 0.000524,  # helium
        #"atmo_kr": 0.000114,  # krypton
    }
    # calculate the mass for each element
    return {label: mass*perc/100 for label, perc in percentages.items()}


def calc_water_storage(volume):
    # the total_capacity is in kg, and it's equal to the volume'
    return {'potable': 0.9 * volume, 'treated': 0.1 * volume}


def convert_configuration(game_config, agent_desc=None, save_output=False):
    """
    This method converts the json configuration from a post into a more 
    complete configuration.

    GRANT: This file underwent a major refactor in May 2023 with the
    splitting-out of `simoc-abm`. Ultimately, these tweaks should be handled
    from the frontend, and the backend should receive a complete, workable
    configuration to feed to the AgentModel. This latest revision is a stop-gap
    with minimal changes to make the transition.
    """
    full_game_config = {  # The object to be returned by this function.
        'agents': {},
        'currencies': {},
        'termination': [],
    }
    # Create a working_config which will be modified by this function, so as
    # not to modify the original.
    working_config = copy.deepcopy(game_config)
    agent_desc = load_data_file('agent_desc.json')
    currency_dict = get_default_currency_data()
    is_b2 = False

    ###########################################################################
    #                   STEP 1: Add non-agent fields                          #
    ###########################################################################

    full_game_config['seed'] = working_config.get('seed', 1000)
    # Sets the start date of the simulation
    if 'start_time' in working_config and isinstance(working_config['start_time'], str):
        start_time = working_config.pop('start_time')
        full_game_config['start_time'] = start_time
    # Sets the length of the simulation
    if 'duration' in working_config and isinstance(working_config['duration'], dict):
        duration = working_config.pop('duration')
        full_game_config['termination'].append({
            'condition': 'time',
            'value': duration.get('value', 0) or 0,
            'unit': duration.get('type', 'day')})
    location = working_config.pop('location', 'mars')
    is_b2 = location == 'b2'
    full_game_config['location'] = 'earth' if is_b2 else location
    # Structures MUST step before other agents so that the `.._ratio` fields
    # are availble for threshold tests.
    if 'priorities' in working_config:
        priorities = working_config.pop('priorities')
        if isinstance(priorities, dict):
            full_game_config['priorities'] = priorities
    if 'priorities' not in full_game_config:
        full_game_config['priorities'] = ['structures', 'storage', 'power_generation',
                                          'inhabitants', 'eclss', 'plants']

    ###########################################################################
    #             STEP 2: Flatten the working_config object                   #
    ###########################################################################
    # The game_config object sent by the frontend has some irregularities. In
    # order to accomodate user-created agents in the future, we first flatten
    # items that are known to be irregular, add some custom information, and
    # then add everything in working_config.

    working_config.pop('single_agent')  # Don't use this anymore

    # Structures: A string under 'habitat' and 'greenhouse' with selected type,
    # and optionally a dict of amounts (area) of b2-specific biomes.
    total_volume = 0  # Used to calculate starting water_storage
    if not is_b2:
        weights = {}
    else:
        startWithM1Atmo = working_config.pop('startWithM1EndingAtmosphere', False)
        if startWithM1Atmo:
            weights = {
                'o2': 14.95,
                'co2': 0.32,
                'h2o': 0.9,
                'n2': 83.83,
                'h2': 0,
                'ch4': 0,
            }
        else:
            weights = {
                'o2': 18.75,  # Estimated from Severinghaus Figure 1
                'co2': 0.04,  # Estimated from Severinghaus Figure 1
                'h2o': 1,     # SIMOC default
                'n2': 80.21,  # Remainder of 100%
                'h2': 0,
                'ch4': 0,
            }
    active_structures = {}
    for generic in {'habitat', 'greenhouse'}:
        if generic in working_config:
            struct = working_config.pop(generic)
            amount = {'greenhouse_b2': 2000, 'crew_habitat_b2': 1000}.get(struct, 1)
            active_structures[struct] = amount
    if is_b2:
        biomes = working_config.pop('biomes', {})
        b2_structs = {'rainforest_biome': biomes.get('rainforest', 2000),
                      'desert_biome': biomes.get('desert', 1400),
                      'ocean_biome': biomes.get('ocean', 863),
                      'savannah_biome': biomes.get('savannah', 1637),
                      'west_lung': 1800,
                      'south_lung': 1800}
        active_structures.update(b2_structs)
    for struct, amount in active_structures.items():
        volume = amount * agent_desc[struct]['properties']['volume']['value']
        total_volume += volume  # Used below to calculate starting water
        atmosphere = calc_air_storage(volume, weights)  # Fill with earth-normal atmosphere
        working_config[struct] = dict(amount=amount, storage=atmosphere)
    if len(active_structures) > 1:
        working_config['atmosphere_equalizer'] = dict(amount=1)

    # Plants: A list of objects with 'species' and 'amount'
    plants_in_config = []
    input_food_storage = working_config.pop('food_storage', None) #### FOOD STORAGE GRABBED ####
    crop_mgmt_input = working_config.pop('improvedCropManagement', False)
    crop_mgmt_factor = 1.5 if crop_mgmt_input is True else 1

    if 'plants' in working_config and isinstance(working_config['plants'], list):
        plants = working_config.pop('plants')
        app.logger.info(f'ZZZZZZZZZZZZZZZZZ PLANTS  ZZZZZZZZZZZZZZZZZ {plants} ' )
        for plant in plants:
            amount = plant.get('amount', 0) or 0
            plant_type = plant.pop('species')
            if not (plant_type and amount):
                continue
            plants_in_config.append(plant_type)
            plant['amount']=amount
            if plant_type not in working_config: # Could be in the working config already as a custom agent
                working_config[plant_type]=plant
            else:
                working_config[plant_type]['amount']=amount
            if is_b2:
                working_config[plant_type]['properties'] = {
                    'crop_management_factor': {'value': crop_mgmt_factor},
                    'density_factor': {'value': 0.5}
                }
        app.logger.info(f'BBBBBBBBBBBBBB WORKING CONFIG  BBBBBBBBBBBBBBBB {working_config} ' )
    # 'plants' in working_config correspond to the ones selected by the user in the wizard,
    # but does not include custom plant agents that were not selected in wizard but may have
    # been sent over as an agent, which need to be removed before simulation start.
    unused_custom_plants = []
    for agent_key, config_agent in working_config.items():
        if 'agent_class' in config_agent and config_agent['agent_class']=='plants':
            if agent_key not in plants_in_config:
                unused_custom_plants.append(agent_key)
    for deletable_plant in unused_custom_plants:
        working_config.pop(deletable_plant)
    # For the plants that are actually in the simulation, add lamps
    if plants_in_config:
        working_config['food_storage'] = dict(amount=1) 
        # Lights
        if is_b2:
            working_config['b2_sun'] = {'amount': 1}
        else:
            # Replace generic lamp with species-specific lamp
            if 'lamp' in working_config:
                del working_config['lamp']
            for species in plants_in_config:
                # Add custom lamp for plant
                lamp_id = f'{species}_lamp'
                working_config[lamp_id] = {'amount': 1, 'prototypes': ['lamp'],
                                           'flows': {'out': {'par': {'connections': [lamp_id]}}}}
                # Add connection to plant 'par' flow
                par_flow_stub = {'in': {'par': {'connections': [lamp_id]}}}
                if 'flows' in working_config[species]:
                    working_config[species]['flows']['in']['par']['connections'].clear() # Remove excess lamps
                    working_config[species]['flows']['in']['par']['connections'].append(lamp_id) # Add specific lamp
                else:
                    working_config[species]['flows']=par_flow_stub
                # Everything else (amt, rate, schedule) managed by LampAgent
     # Default Storages: Some listed, some not. Need to calculate amount.
    # 'food_storage' now holds fresh food, and 'ration_storage' holds the rations. Rations are
    # still pre-loaded to 'food_storage' on the front-end though, so need to change the label.
    if input_food_storage and input_food_storage.get('ration') is not None: # b2: initialize with food (plants) instead of rations
        if is_b2:
            starting_food = input_food_storage['ration']
            greenhouse_layout = {p: working_config[p]['amount'] for p in plants_in_config}
            total_crop_area = sum(greenhouse_layout.values())
            starting_food_storage = {k: starting_food*(v/total_crop_area) for k, v in greenhouse_layout.items()}
            working_config['food_storage']['storage'] = starting_food_storage
        else:
            working_config['ration_storage'] = {
                'amount': input_food_storage.get('amount', 1),
                'storage': {'ration': input_food_storage.get('ration')}}
    for storage_type in ['water_storage', 'nutrient_storage', 'power_storage']:
        if storage_type in working_config and isinstance(working_config[storage_type], dict):
            storage_agent = working_config.pop(storage_type)
            stored_currencies = {k: v for k, v in storage_agent.items() if k in currency_dict}
            storage_agent = {k: v for k, v in storage_agent.items() if k not in currency_dict}
            storage_agent['storage'] = stored_currencies
        else:
            storage_agent = {'storage': {}}
        # Preload water based on habitat & greenhouse volume
        if storage_type == 'water_storage' and total_volume:
            storage_agent['storage'].update(calc_water_storage(total_volume))
        # Determine storage amount based on capacity and starting balance
        if 'amount' in storage_agent and isinstance(storage_agent['amount'], int):
            amount = max(1, storage_agent.pop('amount'))
        else:
            amount = 1
        for field, value in storage_agent['storage'].items():
            if field in agent_desc[storage_type]['capacity']:
                capacity = agent_desc[storage_type]['capacity'][field]
                amount = max(amount, math.ceil(value / capacity))
        storage_agent['amount'] = amount
        working_config[storage_type] = storage_agent    
        
    # For any custom food currencies, it is necessary that food storage be created for them
    # Because food storage is set to a new object above (and combined later with food storage in simoc-abm's 
    # default JSON file), a capacity is defined subobject is defined here.
    custom_currencies = {}
    if 'currencies' in working_config:
        custom_currencies = working_config.pop('currencies') # These are later added to config in format expected by simoc-abm
        working_config['food_storage']['capacity']={};
        # Check if each custom currency is a food, and if it is, add a food storage capacity for this food.
        for currency_name, currency_parameters in custom_currencies.items():
            if 'category' in currency_parameters:
                if currency_parameters['category'] == 'food':
                      #  working_config['food_storage']['capacity']['rice_7']=10000; ## HARD CODED
                       working_config['food_storage']['capacity'][currency_name]=10000 
    # Next, iterate through the custom agents and see if they are a food type agent.
    # If they are, set the capacity to 10,000 which is the default hardcoded amount in the simoc-abm JSON for a food item
    
    
    if 'human_agent' in working_config:
        human = working_config.pop('human_agent')
        working_config['human'] = human
    if 'eclss' in working_config and isinstance(working_config['eclss'], dict):
        eclss = working_config.pop('eclss')
        if is_b2:
            eclss_agents = {
                'solid_waste_aerobic_bioreactor': 1,
                'urine_recycling_processor_VCD': 1,
                'multifiltration_purifier_post_treatment': 50,
                'co2_removal_SAWD': 5,
                'co2_makeup_valve': 5,
                'dehumidifier': 50,
                'o2_makeup_valve': 180,
                'o2_storage': 1,
                'co2_storage': 1,
            }
            for agent, amount in eclss_agents.items():
                working_config[agent] = {'amount': amount}
            # CO2 Control System
            co2UpperLimit = eclss.get('co2UpperLimit', None)
            if co2UpperLimit is not None:
                decimal = round(co2UpperLimit/100, 4)
                sawd_stub = {'in': {'co2': {'criteria': {'in_co2_ratio': {'value': decimal}}}}}
                working_config['co2_removal_SAWD']['flows'] = sawd_stub
            co2Reserves = eclss.get('co2Reserves', None)
            if co2Reserves is not None:
                working_config['co2_storage']['storage'] = {'co2': co2Reserves}
            co2LowerLimit = eclss.get('co2LowerLimit', None)
            if co2LowerLimit is not None:
                decimal = round(co2LowerLimit, 4)
                co2_valve_stub = {'in': {'co2': {'criteria': {'out_co2_ratio': {'value': decimal}}}}}
                working_config['co2_makeup_valve']['flows'] = co2_valve_stub
            # O2 Control System
            o2Reserves = eclss.get('o2Reserves', None)
            if o2Reserves is not None and 'o2_storage' in working_config:
                working_config['o2_storage']['storage'] = {'o2': o2Reserves}
            o2LowerLimit = eclss.get('o2LowerLimit', None)
            if o2LowerLimit is not None and 'o2_makeup_valve' in working_config:
                decimal = round(o2LowerLimit, 4)
                o2_valve_stub = {'in': {'o2': {'criteria': {'out_o2_ratio': {'value': decimal}}}}}
                working_config['o2_makeup_valve']['flows'] = o2_valve_stub

        else:
            # ECLSS: A single item with an amount; needs to be broken into component agents
            eclss_agents = ['solid_waste_aerobic_bioreactor', 'multifiltration_purifier_post_treatment',
                            'oxygen_generation_SFWE', 'urine_recycling_processor_VCD', 'co2_removal_SAWD',
                            'co2_makeup_valve', 'co2_storage', 'co2_reduction_sabatier',
                            'ch4_removal_agent', 'dehumidifier']
            amount = eclss.get('amount', 0) or 0
            if amount:
                plant_area = (0 if not plants_in_config else
                              sum([working_config[p]['amount'] for p in plants_in_config]))
                human_amount = working_config['human']['amount']
                for eclss_agent in eclss_agents:
                    this_amount = amount
                    # Scale certain components based on plant area
                    scale_with_plants = dict(  # Based on minimum required for 4hg preset
                        multifiltration_purifier_post_treatment=3/200,
                        co2_makeup_valve=4/200,
                        dehumidifier=5/200,
                    )
                    if eclss_agent in scale_with_plants:
                        required_amount = round(scale_with_plants[eclss_agent] * plant_area)
                        this_amount = max(this_amount, required_amount)
                    # Scale certain components based on number of humans
                    scale_with_humans = dict(
                        co2_removal_SAWD=2/4,
                    )
                    if eclss_agent in scale_with_humans:
                        required_amount = round(scale_with_humans[eclss_agent] * human_amount)
                        this_amount = max(this_amount, required_amount)
                    working_config[eclss_agent] = dict(amount=this_amount)
    if 'concrete' in working_config:
        carbonation = working_config['concrete'].pop('carbonation', 0)
        working_config['concrete']['attributes'] = {'carbonation': carbonation}
    # 'solar_pv...' is already in the correct format.

    # Update humans to reduce food consumption by 50%
    if is_b2 and 'human' in working_config:
        default_food_rate = agent_desc['human']['flows']['in']['food']['value']
        food_stub = {'in': {'food': {'value': default_food_rate * 0.5}}}
        working_config['human']['flows'] = food_stub


    ###########################################################################
    #                   STEP 3: Add all agents to output                      #
    ###########################################################################


    for agent_id, agent in working_config.items():
        full_game_config['agents'][agent_id] = agent
        
    for currency_id, currency in custom_currencies.items():
            full_game_config['currencies'][currency_id] = currency
    
    # Might need something like this to eliminate excess currency bug:
    #if(plant.custom_flag=true)
        #full_game_config['currencies']= new currency for custom plant
        
    # Print result
    if save_output:
        timestamp = datetime.datetime.now()
        timestamp = timestamp.strftime("%m%d%H%M%S")
        with open(f"full_game_config_{timestamp}.json", "w") as f:
            json.dump(full_game_config, f)

    return full_game_config


def load_from_basedir(fname):
    basedir = pathlib.Path(app.root_path).resolve().parent
    path = basedir / fname
    result = {}
    try:
        with path.open() as f:
            result = json.load(f)
    except OSError as e:
        app.logger.exception(f'Error opening {fname}: {e}')
    except ValueError as e:
        app.logger.exception(f'Error reading {fname}: {e}')
    except Exception as e:
        app.logger.exception(f'Unexpected error handling {fname}: {e}')
    finally:
        return result
