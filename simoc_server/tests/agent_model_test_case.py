import os
import unittest
import tempfile
import time
import threading
import datetime
from simoc_server import db, app
from simoc_server.tests.test_util import setup_db, clear_db
from simoc_server.agent_model import (AgentModel, AgentModelInitializationParams,
    BaseLineAgentInitializerRecipe)
from simoc_server.agent_model.agents import Atmosphere
from simoc_server.database.db_model import AgentModelState

class AgentModelTestCase(unittest.TestCase):

    """Test the functionality of the agent model
    """

    @classmethod
    def setUpClass(cls):
        setup_db()
        cls.default_model_params = AgentModelInitializationParams()
        (cls.default_model_params.set_grid_width(100)
                    .set_grid_height(100)
                    .set_starting_model_time(datetime.timedelta()))
        cls.default_agent_init_recipe = BaseLineAgentInitializerRecipe()

    @classmethod
    def tearDownClass(cls):
        clear_db()

    def testConcurrentBranching(self):
        snapshot_ind = 0
        def snapshot_at_step_1(agent_model_id, branch_ids):
            agent_model_state = AgentModelState.query.get(agent_model_id)
            agent_model = AgentModel.load_from_db(agent_model_state)
            agent_model.step()
            snapshot = agent_model.snapshot()
            branch_ids.append(snapshot.snapshot_branch.id)
        orig_agent_model = AgentModel.create_new(self.default_model_params,
            self.default_agent_init_recipe)
        orig_snapshot_1 = orig_agent_model.snapshot()
        orig_agent_model.step()
        orig_snapshot_2 = orig_agent_model.snapshot()
        model_state_id = orig_snapshot_2.agent_model_state.id
        num_branches = 10
        threads = []
        branch_ids = []
        for i in range(num_branches):
            t=threading.Thread(target=snapshot_at_step_1, args=(model_state_id,branch_ids))
            t.start()
            threads.append(t)
        for thread in threads:
            thread.join()

        for i in range(len(branch_ids)):
            for j in range(i+1, len(branch_ids)):
                self.assertNotEqual(branch_ids[i], branch_ids[j])


    def testTimeDelta(self):
        agent_model = AgentModel.create_new(self.default_model_params,
            self.default_agent_init_recipe)
        for i in range(100):
            agent_model.step()
        delta = agent_model.model_time
        self.assertEqual(delta.days, 4)

    def testSpaceConversion(self):
        agent_model = AgentModel.create_new(self.default_model_params,
            self.default_agent_init_recipe)
        m1 = agent_model.grid_units_to_meters(10)
        self.assertEqual(m1, 10)
        m2 = agent_model.grid_units_to_meters((20, 60))
        self.assertEqual(m2, (20, 60))


    def testAtmosphereGasConversion(self):
        agent_model = AgentModel.create_new(self.default_model_params,
            self.default_agent_init_recipe)

        atmosphere = Atmosphere(agent_model)

        atmosphere.temp = 283
        atmosphere.oxygen = 50
        atmosphere.carbon_dioxide = 30
        atmosphere.volume = 100
        # initial kg 67.9939490102 o2
        # 20 kpa -> moles 849.977486221 -> 27.1975796041 kg o2
        expected_o2_mass_in = 40.7963694061 #67.9939490102 - 27.1975796041
        expected_co2_mass_out = 19.42684257433333
        actual_o2_mass_in, actual_co2_mass_out = atmosphere.convert_o2_to_co2(42, 20, 20)
        self.assertAlmostEqual(actual_o2_mass_in, expected_o2_mass_in, delta=.001, msg="Invalid o2 value in conversion from o2 to co2")
        self.assertAlmostEqual(actual_co2_mass_out, expected_co2_mass_out, delta=.001, msg="Invalid co2 value in from o2 to co2")

        atmosphere.oxygen = 30
        atmosphere.carbon_dioxide = 50

        # intial kg 93.51664797778669 co2
        # 20 kpa -> moles 37.40665919111467 o2
        expected_co2_mass_in = 56.10998878667202
        expected_o2_mass_out = 18.703329595557342
        actual_co2_mass_in, actual_o2_mass_out = atmosphere.convert_co2_to_o2(60, 20, 20)
        self.assertAlmostEqual(actual_co2_mass_in, expected_co2_mass_in, delta=.001, msg="Invalid co2 value in conversion from co2 to o2")
        self.assertAlmostEqual(actual_o2_mass_out, expected_o2_mass_out, delta=.001, msg="Invalid o2 value in from co2 to o2")


if __name__ == "__main__":
    unittest.main()