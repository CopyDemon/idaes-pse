#################################################################################
# The Institute for the Design of Advanced Energy Systems Integrated Platform
# Framework (IDAES IP) was produced under the DOE Institute for the
# Design of Advanced Energy Systems (IDAES).
#
# Copyright (c) 2018-2024 by the software owners: The Regents of the
# University of California, through Lawrence Berkeley National Laboratory,
# National Technology & Engineering Solutions of Sandia, LLC, Carnegie Mellon
# University, West Virginia University Research Corporation, et al.
# All rights reserved.  Please see the files COPYRIGHT.md and LICENSE.md
# for full copyright and license information.
#################################################################################
"""
Tests for ControlVolumeBlockData, and for initializing the moving bed module

Author: Chinedu Okoli
"""

import pytest

from pyomo.environ import (
    ConcreteModel,
    check_optimal_termination,
    SolverStatus,
    value,
    Var,
    Constraint,
    TransformationFactory,
    Reference,
    units as pyo_units,
)
from pyomo.core.expr.visitor import identify_variables
from pyomo.dae.flatten import slice_component_along_sets
from pyomo.util.check_units import assert_units_consistent
from pyomo.common.config import ConfigBlock
from pyomo.common.collections import ComponentSet
from pyomo.util.calc_var_value import calculate_variable_from_constraint
from idaes.core import (
    FlowsheetBlock,
    MaterialBalanceType,
    EnergyBalanceType,
    MomentumBalanceType,
)
from idaes.core.util.model_statistics import (
    degrees_of_freedom,
    number_variables,
    number_total_constraints,
    number_unused_variables,
    unused_variables_set,
)
from idaes.core.util.testing import initialization_tester
from idaes.core.util import scaling as iscale
from idaes.core.solvers import get_solver
from idaes.core.util.exceptions import (
    InitializationError,
    ConfigurationError,
)

# Import MBR unit model
from idaes.models_extra.gas_solid_contactors.unit_models.moving_bed import MBR

# Import property packages
from idaes.models_extra.gas_solid_contactors.properties.methane_iron_OC_reduction.gas_phase_thermo import (
    GasPhaseParameterBlock,
)
from idaes.models_extra.gas_solid_contactors.properties.methane_iron_OC_reduction.solid_phase_thermo import (
    SolidPhaseParameterBlock,
)
from idaes.models_extra.gas_solid_contactors.properties.methane_iron_OC_reduction.hetero_reactions import (
    HeteroReactionParameterBlock,
)

# -----------------------------------------------------------------------------
# Get default solver for testing
solver = get_solver()


# -----------------------------------------------------------------------------
@pytest.mark.unit
def test_config():
    m = ConcreteModel()
    m.fs = FlowsheetBlock(dynamic=False)

    # Set up thermo props and reaction props
    m.fs.gas_properties = GasPhaseParameterBlock()
    m.fs.solid_properties = SolidPhaseParameterBlock()
    m.fs.hetero_reactions = HeteroReactionParameterBlock(
        solid_property_package=m.fs.solid_properties,
        gas_property_package=m.fs.gas_properties,
    )

    m.fs.unit = MBR(
        gas_phase_config={"property_package": m.fs.gas_properties},
        solid_phase_config={
            "property_package": m.fs.solid_properties,
            "reaction_package": m.fs.hetero_reactions,
        },
    )

    # Check unit config arguments
    assert len(m.fs.unit.config) == 17
    assert isinstance(m.fs.unit.config.gas_phase_config, ConfigBlock)
    assert isinstance(m.fs.unit.config.solid_phase_config, ConfigBlock)

    assert m.fs.unit.config.finite_elements == 10
    assert m.fs.unit.config.length_domain_set == [0.0, 1.0]
    assert m.fs.unit.config.transformation_method == "dae.finite_difference"
    assert m.fs.unit.config.transformation_scheme == "BACKWARD"
    assert m.fs.unit.config.collocation_points == 3
    assert m.fs.unit.config.flow_type == "counter_current"
    assert m.fs.unit.config.material_balance_type == MaterialBalanceType.componentTotal
    assert m.fs.unit.config.energy_balance_type == EnergyBalanceType.enthalpyTotal
    assert m.fs.unit.config.momentum_balance_type == MomentumBalanceType.pressureTotal
    assert m.fs.unit.config.has_pressure_change is True

    # Check gas phase config arguments
    assert len(m.fs.unit.config.gas_phase_config) == 7
    assert m.fs.unit.config.gas_phase_config.has_equilibrium_reactions is False
    assert m.fs.unit.config.gas_phase_config.property_package is m.fs.gas_properties
    assert m.fs.unit.config.gas_phase_config.reaction_package is None

    # Check solid phase config arguments
    assert len(m.fs.unit.config.solid_phase_config) == 7
    assert m.fs.unit.config.solid_phase_config.has_equilibrium_reactions is False
    assert m.fs.unit.config.solid_phase_config.property_package is m.fs.solid_properties
    assert m.fs.unit.config.solid_phase_config.reaction_package is m.fs.hetero_reactions


# -----------------------------------------------------------------------------
class TestIronOC(object):
    @pytest.fixture(scope="class")
    def iron_oc(self):
        m = ConcreteModel()
        m.fs = FlowsheetBlock(dynamic=False)

        # Set up thermo props and reaction props
        m.fs.gas_properties = GasPhaseParameterBlock()
        m.fs.solid_properties = SolidPhaseParameterBlock()
        m.fs.hetero_reactions = HeteroReactionParameterBlock(
            solid_property_package=m.fs.solid_properties,
            gas_property_package=m.fs.gas_properties,
        )

        m.fs.unit = MBR(
            gas_phase_config={"property_package": m.fs.gas_properties},
            solid_phase_config={
                "property_package": m.fs.solid_properties,
                "reaction_package": m.fs.hetero_reactions,
            },
        )

        # Fix geometry variables
        m.fs.unit.bed_diameter.fix(6.5)  # m
        m.fs.unit.bed_height.fix(5)  # m

        # Fix inlet port variables for gas and solid
        m.fs.unit.gas_inlet.flow_mol[0].fix(128.20513)  # mol/s
        m.fs.unit.gas_inlet.temperature[0].fix(298.15)  # K
        m.fs.unit.gas_inlet.pressure[0].fix(2.00e5)  # Pa = 1E5 bar
        m.fs.unit.gas_inlet.mole_frac_comp[0, "CO2"].fix(0.02499)
        m.fs.unit.gas_inlet.mole_frac_comp[0, "H2O"].fix(0.00001)
        m.fs.unit.gas_inlet.mole_frac_comp[0, "CH4"].fix(0.975)

        m.fs.unit.solid_inlet.flow_mass[0].fix(591.4)  # kg/s
        m.fs.unit.solid_inlet.particle_porosity[0].fix(0.27)  # (-)
        m.fs.unit.solid_inlet.temperature[0].fix(1183.15)  # K
        m.fs.unit.solid_inlet.mass_frac_comp[0, "Fe2O3"].fix(0.45)
        m.fs.unit.solid_inlet.mass_frac_comp[0, "Fe3O4"].fix(1e-9)
        m.fs.unit.solid_inlet.mass_frac_comp[0, "Al2O3"].fix(0.55)

        return m

    @pytest.mark.build
    @pytest.mark.unit
    def test_build(self, iron_oc):
        assert hasattr(iron_oc.fs.unit, "gas_inlet")
        assert len(iron_oc.fs.unit.gas_inlet.vars) == 4
        assert isinstance(iron_oc.fs.unit.gas_inlet.flow_mol, Var)
        assert isinstance(iron_oc.fs.unit.gas_inlet.mole_frac_comp, Var)
        assert isinstance(iron_oc.fs.unit.gas_inlet.temperature, Var)
        assert isinstance(iron_oc.fs.unit.gas_inlet.pressure, Var)

        assert hasattr(iron_oc.fs.unit, "solid_inlet")
        assert len(iron_oc.fs.unit.solid_inlet.vars) == 4
        assert isinstance(iron_oc.fs.unit.solid_inlet.flow_mass, Var)
        assert isinstance(iron_oc.fs.unit.solid_inlet.particle_porosity, Var)
        assert isinstance(iron_oc.fs.unit.solid_inlet.mass_frac_comp, Var)
        assert isinstance(iron_oc.fs.unit.solid_inlet.temperature, Var)

        assert hasattr(iron_oc.fs.unit, "gas_outlet")
        assert len(iron_oc.fs.unit.gas_outlet.vars) == 4
        assert isinstance(iron_oc.fs.unit.gas_outlet.flow_mol, Var)
        assert isinstance(iron_oc.fs.unit.gas_outlet.mole_frac_comp, Var)
        assert isinstance(iron_oc.fs.unit.gas_outlet.temperature, Var)
        assert isinstance(iron_oc.fs.unit.gas_outlet.pressure, Var)

        assert hasattr(iron_oc.fs.unit, "solid_outlet")
        assert len(iron_oc.fs.unit.solid_outlet.vars) == 4
        assert isinstance(iron_oc.fs.unit.solid_outlet.flow_mass, Var)
        assert isinstance(iron_oc.fs.unit.solid_outlet.particle_porosity, Var)
        assert isinstance(iron_oc.fs.unit.solid_outlet.mass_frac_comp, Var)
        assert isinstance(iron_oc.fs.unit.solid_outlet.temperature, Var)

        assert isinstance(iron_oc.fs.unit.bed_area_eqn, Constraint)
        assert isinstance(iron_oc.fs.unit.gas_phase_area, Constraint)
        assert isinstance(iron_oc.fs.unit.solid_phase_area, Constraint)
        assert isinstance(iron_oc.fs.unit.gas_super_vel, Constraint)
        assert isinstance(iron_oc.fs.unit.solid_super_vel, Constraint)
        assert isinstance(iron_oc.fs.unit.gas_phase_config_pressure_drop, Constraint)
        assert isinstance(iron_oc.fs.unit.gas_solid_htc_eqn, Constraint)
        assert isinstance(iron_oc.fs.unit.gas_phase_heat_transfer, Constraint)
        assert isinstance(iron_oc.fs.unit.solid_phase_config_rxn_ext, Constraint)
        assert isinstance(iron_oc.fs.unit.gas_comp_hetero_rxn, Constraint)

        assert number_variables(iron_oc) == 819
        assert number_total_constraints(iron_oc) == 781
        assert number_unused_variables(iron_oc) == 16

    @pytest.mark.unit
    def test_dof(self, iron_oc):
        assert degrees_of_freedom(iron_oc) == 0

    @pytest.fixture(scope="class")
    def iron_oc_unscaled(self):
        m = ConcreteModel()
        m.fs = FlowsheetBlock(dynamic=False)

        # Set up thermo props and reaction props
        m.fs.gas_properties = GasPhaseParameterBlock()
        m.fs.solid_properties = SolidPhaseParameterBlock()
        m.fs.hetero_reactions = HeteroReactionParameterBlock(
            solid_property_package=m.fs.solid_properties,
            gas_property_package=m.fs.gas_properties,
        )

        m.fs.unit = MBR(
            gas_phase_config={"property_package": m.fs.gas_properties},
            solid_phase_config={
                "property_package": m.fs.solid_properties,
                "reaction_package": m.fs.hetero_reactions,
            },
        )

        # Fix geometry variables
        m.fs.unit.bed_diameter.fix(6.5)  # m
        m.fs.unit.bed_height.fix(5)  # m

        # Fix inlet port variables for gas and solid
        m.fs.unit.gas_inlet.flow_mol[0].fix(128.20513)  # mol/s
        m.fs.unit.gas_inlet.temperature[0].fix(298.15)  # K
        m.fs.unit.gas_inlet.pressure[0].fix(2.00e5)  # Pa = 1E5 bar
        m.fs.unit.gas_inlet.mole_frac_comp[0, "CO2"].fix(0.02499)
        m.fs.unit.gas_inlet.mole_frac_comp[0, "H2O"].fix(0.00001)
        m.fs.unit.gas_inlet.mole_frac_comp[0, "CH4"].fix(0.975)

        m.fs.unit.solid_inlet.flow_mass[0].fix(591.4)  # kg/s
        m.fs.unit.solid_inlet.particle_porosity[0].fix(0.27)  # (-)
        m.fs.unit.solid_inlet.temperature[0].fix(1183.15)  # K
        m.fs.unit.solid_inlet.mass_frac_comp[0, "Fe2O3"].fix(0.45)
        m.fs.unit.solid_inlet.mass_frac_comp[0, "Fe3O4"].fix(1e-9)
        m.fs.unit.solid_inlet.mass_frac_comp[0, "Al2O3"].fix(0.55)

        return m

    @pytest.mark.solver
    @pytest.mark.skipif(solver is None, reason="Solver not available")
    @pytest.mark.component
    def test_initialize_unscaled(self, iron_oc_unscaled):
        initialization_tester(
            iron_oc_unscaled,
            optarg={"tol": 1e-6},
            gas_phase_state_args={
                "flow_mol": 128.20513,
                "temperature": 1183.15,
                "pressure": 2.00e5,
            },
            solid_phase_state_args={"flow_mass": 591.4, "temperature": 1183.15},
        )

    @pytest.mark.solver
    @pytest.mark.skipif(solver is None, reason="Solver not available")
    @pytest.mark.component
    def test_solve_unscaled(self, iron_oc_unscaled):
        results = solver.solve(iron_oc_unscaled)

        # Check for optimal solution
        assert check_optimal_termination(results)
        assert results.solver.status == SolverStatus.ok

    @pytest.mark.component
    def test_scaling(self, iron_oc):

        # Set scaling gas phase for state variables
        iron_oc.fs.gas_properties.set_default_scaling("flow_mol", 1e-3)
        iron_oc.fs.gas_properties.set_default_scaling("pressure", 1e-5)
        iron_oc.fs.gas_properties.set_default_scaling("temperature", 1e-2)
        for comp in iron_oc.fs.gas_properties.component_list:
            iron_oc.fs.gas_properties.set_default_scaling(
                "mole_frac_comp", 1e1, index=comp
            )
        # Set scaling for gas phase thermophysical and transport properties
        iron_oc.fs.gas_properties.set_default_scaling("enth_mol", 1e-6)
        iron_oc.fs.gas_properties.set_default_scaling("enth_mol_comp", 1e-6)
        iron_oc.fs.gas_properties.set_default_scaling("cp_mol", 1e-6)
        iron_oc.fs.gas_properties.set_default_scaling("cp_mol_comp", 1e-6)
        iron_oc.fs.gas_properties.set_default_scaling("cp_mass", 1e-6)
        iron_oc.fs.gas_properties.set_default_scaling("entr_mol", 1e-2)
        iron_oc.fs.gas_properties.set_default_scaling("entr_mol_phase", 1e-2)
        iron_oc.fs.gas_properties.set_default_scaling("dens_mol", 1)
        iron_oc.fs.gas_properties.set_default_scaling("dens_mol_comp", 1)
        iron_oc.fs.gas_properties.set_default_scaling("dens_mass", 1e2)
        iron_oc.fs.gas_properties.set_default_scaling("visc_d_comp", 1e4)
        iron_oc.fs.gas_properties.set_default_scaling("diffusion_comp", 1e5)
        iron_oc.fs.gas_properties.set_default_scaling("therm_cond_comp", 1e2)
        iron_oc.fs.gas_properties.set_default_scaling("visc_d", 1e5)
        iron_oc.fs.gas_properties.set_default_scaling("therm_cond", 1e0)
        iron_oc.fs.gas_properties.set_default_scaling("mw", 1e2)

        # Set scaling for solid phase state variables
        iron_oc.fs.solid_properties.set_default_scaling("flow_mass", 1e-3)
        iron_oc.fs.solid_properties.set_default_scaling("particle_porosity", 1e2)
        iron_oc.fs.solid_properties.set_default_scaling("temperature", 1e-2)
        for comp in iron_oc.fs.solid_properties.component_list:
            iron_oc.fs.solid_properties.set_default_scaling(
                "mass_frac_comp", 1e1, index=comp
            )

        # Set scaling for solid phase thermophysical and transport properties
        iron_oc.fs.solid_properties.set_default_scaling("enth_mass", 1e-6)
        iron_oc.fs.solid_properties.set_default_scaling("enth_mol_comp", 1e-6)
        iron_oc.fs.solid_properties.set_default_scaling("cp_mol_comp", 1e-6)
        iron_oc.fs.solid_properties.set_default_scaling("cp_mass", 1e-6)
        iron_oc.fs.solid_properties.set_default_scaling("dens_mass_particle", 1e-2)
        iron_oc.fs.solid_properties.set_default_scaling("dens_mass_skeletal", 1e-2)

        MB = iron_oc.fs.unit  # alias to keep test lines short

        # Calculate scaling factors
        iscale.calculate_scaling_factors(MB)

        assert pytest.approx(0.01538, abs=1e-2) == iscale.get_scaling_factor(
            MB.bed_diameter
        )
        assert pytest.approx(0.003014, abs=1e-2) == iscale.get_scaling_factor(
            MB.bed_area
        )
        assert pytest.approx(0.003014, abs=1e-2) == iscale.get_scaling_factor(
            MB.gas_phase.area
        )
        assert pytest.approx(0.003014, abs=1e-2) == iscale.get_scaling_factor(
            MB.solid_phase.area
        )
        assert pytest.approx(0.068, abs=1e-2) == iscale.get_scaling_factor(
            MB.gas_solid_htc[0, 0]
        )
        assert pytest.approx(6666.67, abs=1e-2) == iscale.get_scaling_factor(
            MB.Re_particle[0, 0]
        )
        assert pytest.approx(0.099, abs=1e-2) == iscale.get_scaling_factor(
            MB.Pr_particle[0, 0]
        )
        assert pytest.approx(0.06858, abs=1e-2) == iscale.get_scaling_factor(
            MB.Nu_particle[0, 0]
        )
        for (t, x), v in MB.gas_phase.heat.items():
            assert pytest.approx(0.001, abs=1e-2) == iscale.get_scaling_factor(v)
        for (t, x), v in MB.solid_phase.heat.items():
            assert pytest.approx(0.001, abs=1e-2) == iscale.get_scaling_factor(v)

        for c in MB.bed_area_eqn.values():
            assert pytest.approx(
                0.0030, abs=1e-2
            ) == iscale.get_constraint_transform_applied_scaling_factor(c)
        for (t, x), c in MB.gas_phase_area.items():
            assert pytest.approx(
                0.0030, abs=1e-2
            ) == iscale.get_constraint_transform_applied_scaling_factor(c)
        for (t, x), c in MB.solid_phase_area.items():
            assert pytest.approx(
                0.0030, abs=1e-2
            ) == iscale.get_constraint_transform_applied_scaling_factor(c)
        for (t, x), c in MB.gas_super_vel.items():
            assert pytest.approx(
                0.001, abs=1e-2
            ) == iscale.get_constraint_transform_applied_scaling_factor(c)
        for (t, x), c in MB.solid_super_vel.items():
            assert pytest.approx(
                0.0001, abs=1e-2
            ) == iscale.get_constraint_transform_applied_scaling_factor(c)
        for (t, x), c in MB.gas_phase_config_pressure_drop.items():
            assert pytest.approx(
                0.0001, abs=1e-2
            ) == iscale.get_constraint_transform_applied_scaling_factor(c)
        for (t, x, r), c in MB.solid_phase_config_rxn_ext.items():
            assert pytest.approx(
                3.0135e-5, abs=1e-4
            ) == iscale.get_constraint_transform_applied_scaling_factor(c)
        for (t, x, p, j), c in MB.gas_comp_hetero_rxn.items():
            assert pytest.approx(
                0.01, abs=1e-2
            ) == iscale.get_constraint_transform_applied_scaling_factor(c)
        for (t, x), c in MB.solid_phase_heat_transfer.items():
            assert pytest.approx(
                1e-9, abs=1e-8
            ) == iscale.get_constraint_transform_applied_scaling_factor(c)
        for (t, x), c in MB.reynolds_number_particle.items():
            assert pytest.approx(
                6666, abs=100
            ) == iscale.get_constraint_transform_applied_scaling_factor(c)
        for (t, x), c in MB.prandtl_number.items():
            assert pytest.approx(
                0.1, abs=1e-2
            ) == iscale.get_constraint_transform_applied_scaling_factor(c)
        for (t, x), c in MB.nusselt_number_particle.items():
            assert pytest.approx(
                0.07, abs=1e-2
            ) == iscale.get_constraint_transform_applied_scaling_factor(c)
        for (t, x), c in MB.gas_solid_htc_eqn.items():
            assert pytest.approx(
                0.07, abs=1e-2
            ) == iscale.get_constraint_transform_applied_scaling_factor(c)
        for (t, x), c in MB.gas_phase_heat_transfer.items():
            assert pytest.approx(
                1e-9, abs=1e-8
            ) == iscale.get_constraint_transform_applied_scaling_factor(c)

    @pytest.mark.solver
    @pytest.mark.skipif(solver is None, reason="Solver not available")
    @pytest.mark.component
    def test_initialize(self, iron_oc):
        initialization_tester(
            iron_oc,
            optarg={"tol": 1e-6},
            gas_phase_state_args={
                "flow_mol": 128.20513,
                "temperature": 1183.15,
                "pressure": 2.00e5,
            },
            solid_phase_state_args={"flow_mass": 591.4, "temperature": 1183.15},
        )

    @pytest.mark.solver
    @pytest.mark.skipif(solver is None, reason="Solver not available")
    @pytest.mark.component
    def test_solve(self, iron_oc):
        results = solver.solve(iron_oc)

        # Check for optimal solution
        assert check_optimal_termination(results)
        assert results.solver.status == SolverStatus.ok

    @pytest.mark.solver
    @pytest.mark.skipif(solver is None, reason="Solver not available")
    @pytest.mark.component
    def test_solution(self, iron_oc):
        assert (
            pytest.approx(0.0479, abs=1e-2)
            == iron_oc.fs.unit.velocity_superficial_gas[0, 0].value
        )
        assert (
            pytest.approx(0.5675, abs=1e-2)
            == iron_oc.fs.unit.velocity_superficial_gas[0, 1].value
        )
        assert (
            pytest.approx(0.0039, abs=1e-2)
            == iron_oc.fs.unit.velocity_superficial_solid[0].value
        )
        # Check the pressure drop that occurs across the bed
        assert (
            pytest.approx(198217.7068, abs=1e-2)
            == iron_oc.fs.unit.gas_outlet.pressure[0].value
        )
        assert (
            pytest.approx(1782.2932, abs=1e-2)
            == iron_oc.fs.unit.gas_inlet.pressure[0].value
            - iron_oc.fs.unit.gas_outlet.pressure[0].value
        )

    @pytest.mark.component
    def test_units_consistent(self, iron_oc):
        assert_units_consistent(iron_oc)

    @pytest.mark.solver
    @pytest.mark.skipif(solver is None, reason="Solver not available")
    @pytest.mark.component
    def test_conservation(self, iron_oc):
        # Conservation of material check
        calculate_variable_from_constraint(
            iron_oc.fs.unit.gas_phase.properties[0, 0].mw,
            iron_oc.fs.unit.gas_phase.properties[0, 0].mw_eqn,
        )
        calculate_variable_from_constraint(
            iron_oc.fs.unit.gas_phase.properties[0, 1].mw,
            iron_oc.fs.unit.gas_phase.properties[0, 1].mw_eqn,
        )
        mbal_gas = value(
            (
                iron_oc.fs.unit.gas_inlet.flow_mol[0]
                * iron_oc.fs.unit.gas_phase.properties[0, 0].mw
            )
            - (
                iron_oc.fs.unit.gas_outlet.flow_mol[0]
                * iron_oc.fs.unit.gas_phase.properties[0, 1].mw
            )
        )
        mbal_solid = value(
            iron_oc.fs.unit.solid_inlet.flow_mass[0]
            - iron_oc.fs.unit.solid_outlet.flow_mass[0]
        )
        mbal_tol = mbal_gas + mbal_solid
        assert abs(mbal_tol) <= 1e-2

        # Reaction stoichiometric ratio check
        # Overall reducer reactions for methane combustion:
        # CH4 + 12Fe2O3 => 8Fe3O4 + CO2 + 2H2O
        mole_gas_reacted = value(
            iron_oc.fs.unit.gas_inlet.flow_mol[0]
            * iron_oc.fs.unit.gas_inlet.mole_frac_comp[0, "CH4"]
            - iron_oc.fs.unit.gas_outlet.flow_mol[0]
            * iron_oc.fs.unit.gas_outlet.mole_frac_comp[0, "CH4"]
        )
        mole_solid_reacted = value(
            (
                iron_oc.fs.unit.solid_inlet.flow_mass[0]
                * iron_oc.fs.unit.solid_inlet.mass_frac_comp[0, "Fe2O3"]
                / iron_oc.fs.unit.solid_phase.properties[0, 1]._params.mw_comp["Fe2O3"]
            )
            - (
                iron_oc.fs.unit.solid_outlet.flow_mass[0]
                * iron_oc.fs.unit.solid_outlet.mass_frac_comp[0, "Fe2O3"]
                / iron_oc.fs.unit.solid_phase.properties[0, 0]._params.mw_comp["Fe2O3"]
            )
        )
        stoichiometric_ratio = mole_solid_reacted / mole_gas_reacted
        assert pytest.approx(12, abs=1e-6) == stoichiometric_ratio

        # Conservation of energy check
        ebal_gas = value(
            (
                iron_oc.fs.unit.gas_inlet.flow_mol[0]
                * iron_oc.fs.unit.gas_phase.properties[0, 0].enth_mol
            )
            - (
                iron_oc.fs.unit.gas_outlet.flow_mol[0]
                * iron_oc.fs.unit.gas_phase.properties[0, 1].enth_mol
            )
        )
        ebal_solid = value(
            (
                iron_oc.fs.unit.solid_inlet.flow_mass[0]
                * iron_oc.fs.unit.solid_phase.properties[0, 1].enth_mass
            )
            - (
                iron_oc.fs.unit.solid_outlet.flow_mass[0]
                * iron_oc.fs.unit.solid_phase.properties[0, 0].enth_mass
            )
        )
        e_reaction = value(
            mole_gas_reacted
            * iron_oc.fs.unit.solid_phase.reactions[0, 1]._params.dh_rxn["R1"]
        )
        ebal_tol = ebal_gas + ebal_solid - e_reaction
        assert abs(ebal_tol) <= 1e-2

    @pytest.mark.ui
    @pytest.mark.unit
    def test_report(self, iron_oc):
        iron_oc.fs.unit.report()

    @pytest.mark.component
    def test_initialization_error(self, iron_oc):
        iron_oc.fs.unit.gas_outlet.flow_mol[0].fix(1)

        with pytest.raises(InitializationError):
            iron_oc.fs.unit.initialize()


# -----------------------------------------------------------------------------
class TestIronOC_EnergyBalanceType(object):
    @pytest.fixture(scope="class")
    def iron_oc(self):
        m = ConcreteModel()
        m.fs = FlowsheetBlock(dynamic=False)

        # Set up thermo props and reaction props
        m.fs.gas_properties = GasPhaseParameterBlock()
        m.fs.solid_properties = SolidPhaseParameterBlock()
        m.fs.hetero_reactions = HeteroReactionParameterBlock(
            solid_property_package=m.fs.solid_properties,
            gas_property_package=m.fs.gas_properties,
        )

        m.fs.unit = MBR(
            energy_balance_type=EnergyBalanceType.none,
            gas_phase_config={"property_package": m.fs.gas_properties},
            solid_phase_config={
                "property_package": m.fs.solid_properties,
                "reaction_package": m.fs.hetero_reactions,
            },
        )

        # Fix geometry variables
        m.fs.unit.bed_diameter.fix(6.5)  # m
        m.fs.unit.bed_height.fix(5)  # m

        # Fix inlet port variables for gas and solid
        m.fs.unit.gas_inlet.flow_mol[0].fix(128.20513)  # mol/s
        m.fs.unit.gas_inlet.temperature[0].fix(1183.15)  # K
        m.fs.unit.gas_inlet.pressure[0].fix(2.00e5)  # Pa = 1E5 bar
        m.fs.unit.gas_inlet.mole_frac_comp[0, "CO2"].fix(0.02499)
        m.fs.unit.gas_inlet.mole_frac_comp[0, "H2O"].fix(0.00001)
        m.fs.unit.gas_inlet.mole_frac_comp[0, "CH4"].fix(0.975)

        m.fs.unit.solid_inlet.flow_mass[0].fix(591.4)  # kg/s
        m.fs.unit.solid_inlet.temperature[0].fix(1183.15)  # K
        m.fs.unit.solid_inlet.particle_porosity[0].fix(0.27)  # (-)
        m.fs.unit.solid_inlet.mass_frac_comp[0, "Fe2O3"].fix(0.45)
        m.fs.unit.solid_inlet.mass_frac_comp[0, "Fe3O4"].fix(1e-9)
        m.fs.unit.solid_inlet.mass_frac_comp[0, "Al2O3"].fix(0.55)

        return m

    @pytest.mark.build
    @pytest.mark.unit
    def test_build(self, iron_oc):
        assert hasattr(iron_oc.fs.unit, "gas_inlet")
        assert len(iron_oc.fs.unit.gas_inlet.vars) == 4
        assert isinstance(iron_oc.fs.unit.gas_inlet.flow_mol, Var)
        assert isinstance(iron_oc.fs.unit.gas_inlet.mole_frac_comp, Var)
        assert isinstance(iron_oc.fs.unit.gas_inlet.temperature, Var)
        assert isinstance(iron_oc.fs.unit.gas_inlet.pressure, Var)

        assert hasattr(iron_oc.fs.unit, "solid_inlet")
        assert len(iron_oc.fs.unit.solid_inlet.vars) == 4
        assert isinstance(iron_oc.fs.unit.solid_inlet.flow_mass, Var)
        assert isinstance(iron_oc.fs.unit.solid_inlet.particle_porosity, Var)
        assert isinstance(iron_oc.fs.unit.solid_inlet.mass_frac_comp, Var)
        assert isinstance(iron_oc.fs.unit.solid_inlet.temperature, Var)

        assert hasattr(iron_oc.fs.unit, "gas_outlet")
        assert len(iron_oc.fs.unit.gas_outlet.vars) == 4
        assert isinstance(iron_oc.fs.unit.gas_outlet.flow_mol, Var)
        assert isinstance(iron_oc.fs.unit.gas_outlet.mole_frac_comp, Var)
        assert isinstance(iron_oc.fs.unit.gas_outlet.temperature, Var)
        assert isinstance(iron_oc.fs.unit.gas_outlet.pressure, Var)

        assert hasattr(iron_oc.fs.unit, "solid_outlet")
        assert len(iron_oc.fs.unit.solid_outlet.vars) == 4
        assert isinstance(iron_oc.fs.unit.solid_outlet.flow_mass, Var)
        assert isinstance(iron_oc.fs.unit.solid_outlet.particle_porosity, Var)
        assert isinstance(iron_oc.fs.unit.solid_outlet.mass_frac_comp, Var)
        assert isinstance(iron_oc.fs.unit.solid_outlet.temperature, Var)

        assert isinstance(iron_oc.fs.unit.isothermal_gas_phase, Constraint)
        assert isinstance(iron_oc.fs.unit.isothermal_solid_phase, Constraint)

        assert number_variables(iron_oc) == 588
        assert number_total_constraints(iron_oc) == 508
        assert number_unused_variables(iron_oc) == 59
        print(unused_variables_set(iron_oc))

    @pytest.mark.unit
    def test_dof(self, iron_oc):
        assert degrees_of_freedom(iron_oc) == 0

    @pytest.fixture(scope="class")
    def iron_oc_unscaled(self, iron_oc):
        m = ConcreteModel()
        m.fs = FlowsheetBlock(dynamic=False)

        # Set up thermo props and reaction props
        m.fs.gas_properties = GasPhaseParameterBlock()
        m.fs.solid_properties = SolidPhaseParameterBlock()
        m.fs.hetero_reactions = HeteroReactionParameterBlock(
            solid_property_package=m.fs.solid_properties,
            gas_property_package=m.fs.gas_properties,
        )

        m.fs.unit = MBR(
            energy_balance_type=EnergyBalanceType.none,
            gas_phase_config={"property_package": m.fs.gas_properties},
            solid_phase_config={
                "property_package": m.fs.solid_properties,
                "reaction_package": m.fs.hetero_reactions,
            },
        )

        # Fix geometry variables
        m.fs.unit.bed_diameter.fix(6.5)  # m
        m.fs.unit.bed_height.fix(5)  # m

        # Fix inlet port variables for gas and solid
        m.fs.unit.gas_inlet.flow_mol[0].fix(128.20513)  # mol/s
        m.fs.unit.gas_inlet.temperature[0].fix(1183.15)  # K
        m.fs.unit.gas_inlet.pressure[0].fix(2.00e5)  # Pa = 1E5 bar
        m.fs.unit.gas_inlet.mole_frac_comp[0, "CO2"].fix(0.02499)
        m.fs.unit.gas_inlet.mole_frac_comp[0, "H2O"].fix(0.00001)
        m.fs.unit.gas_inlet.mole_frac_comp[0, "CH4"].fix(0.975)

        m.fs.unit.solid_inlet.flow_mass[0].fix(591.4)  # kg/s
        m.fs.unit.solid_inlet.temperature[0].fix(1183.15)  # K
        m.fs.unit.solid_inlet.particle_porosity[0].fix(0.27)  # (-)
        m.fs.unit.solid_inlet.mass_frac_comp[0, "Fe2O3"].fix(0.45)
        m.fs.unit.solid_inlet.mass_frac_comp[0, "Fe3O4"].fix(1e-9)
        m.fs.unit.solid_inlet.mass_frac_comp[0, "Al2O3"].fix(0.55)

        return m

    @pytest.mark.solver
    @pytest.mark.skipif(solver is None, reason="Solver not available")
    @pytest.mark.component
    def test_initialize_unscaled(self, iron_oc_unscaled):
        initialization_tester(
            iron_oc_unscaled,
            optarg={"tol": 1e-6},
            gas_phase_state_args={
                "flow_mol": 128.20513,
                "temperature": 1183.15,
                "pressure": 2.00e5,
            },
            solid_phase_state_args={"flow_mass": 591.4, "temperature": 1183.15},
        )

    @pytest.mark.solver
    @pytest.mark.skipif(solver is None, reason="Solver not available")
    @pytest.mark.component
    def test_solve_unscaled(self, iron_oc_unscaled):
        results = solver.solve(iron_oc_unscaled)

        # Check for optimal solution
        assert check_optimal_termination(results)
        assert results.solver.status == SolverStatus.ok

    @pytest.mark.solver
    @pytest.mark.skipif(solver is None, reason="Solver not available")
    @pytest.mark.component
    def test_initialize(self, iron_oc):
        iscale.calculate_scaling_factors(iron_oc.fs.unit)
        initialization_tester(
            iron_oc,
            optarg={"tol": 1e-6},
            gas_phase_state_args={
                "flow_mol": 128.20513,
                "temperature": 1183.15,
                "pressure": 2.00e5,
            },
            solid_phase_state_args={"flow_mass": 591.4, "temperature": 1183.15},
        )

    @pytest.mark.solver
    @pytest.mark.skipif(solver is None, reason="Solver not available")
    @pytest.mark.component
    def test_solve_scaled(self, iron_oc):
        results = solver.solve(iron_oc)

        # Check for optimal solution
        assert check_optimal_termination(results)
        assert results.solver.status == SolverStatus.ok

    @pytest.mark.solver
    @pytest.mark.skipif(solver is None, reason="Solver not available")
    @pytest.mark.component
    def test_solution_scaled(self, iron_oc):
        assert (
            pytest.approx(0.1900, abs=1e-2)
            == iron_oc.fs.unit.velocity_superficial_gas[0, 0].value
        )
        assert (
            pytest.approx(0.5675, abs=1e-2)
            == iron_oc.fs.unit.velocity_superficial_gas[0, 1].value
        )
        assert (
            pytest.approx(0.0039, abs=1e-2)
            == iron_oc.fs.unit.velocity_superficial_solid[0].value
        )
        # Check the pressure drop that occurs across the bed
        assert (
            pytest.approx(198214.8255, abs=1e-2)
            == iron_oc.fs.unit.gas_outlet.pressure[0].value
        )
        assert (
            pytest.approx(1785.1745, abs=1e-2)
            == iron_oc.fs.unit.gas_inlet.pressure[0].value
            - iron_oc.fs.unit.gas_outlet.pressure[0].value
        )

    @pytest.mark.component
    def test_units_consistent(self, iron_oc):
        assert_units_consistent(iron_oc)

    @pytest.mark.solver
    @pytest.mark.skipif(solver is None, reason="Solver not available")
    @pytest.mark.component
    def test_conservation(self, iron_oc):
        # Conservation of material check
        calculate_variable_from_constraint(
            iron_oc.fs.unit.gas_phase.properties[0, 0].mw,
            iron_oc.fs.unit.gas_phase.properties[0, 0].mw_eqn,
        )
        calculate_variable_from_constraint(
            iron_oc.fs.unit.gas_phase.properties[0, 1].mw,
            iron_oc.fs.unit.gas_phase.properties[0, 1].mw_eqn,
        )
        mbal_gas = value(
            (
                iron_oc.fs.unit.gas_inlet.flow_mol[0]
                * iron_oc.fs.unit.gas_phase.properties[0, 0].mw
            )
            - (
                iron_oc.fs.unit.gas_outlet.flow_mol[0]
                * iron_oc.fs.unit.gas_phase.properties[0, 1].mw
            )
        )
        mbal_solid = value(
            iron_oc.fs.unit.solid_inlet.flow_mass[0]
            - iron_oc.fs.unit.solid_outlet.flow_mass[0]
        )
        mbal_tol = mbal_gas + mbal_solid
        assert abs(mbal_tol) <= 1e-2

        # Reaction stoichiometric ratio check
        # Overall reducer reactions for methane combustion:
        # CH4 + 12Fe2O3 => 8Fe3O4 + CO2 + 2H2O
        mole_gas_reacted = value(
            iron_oc.fs.unit.gas_inlet.flow_mol[0]
            * iron_oc.fs.unit.gas_inlet.mole_frac_comp[0, "CH4"]
            - iron_oc.fs.unit.gas_outlet.flow_mol[0]
            * iron_oc.fs.unit.gas_outlet.mole_frac_comp[0, "CH4"]
        )
        mole_solid_reacted = value(
            (
                iron_oc.fs.unit.solid_inlet.flow_mass[0]
                * iron_oc.fs.unit.solid_inlet.mass_frac_comp[0, "Fe2O3"]
                / iron_oc.fs.unit.solid_phase.properties[0, 1]._params.mw_comp["Fe2O3"]
            )
            - (
                iron_oc.fs.unit.solid_outlet.flow_mass[0]
                * iron_oc.fs.unit.solid_outlet.mass_frac_comp[0, "Fe2O3"]
                / iron_oc.fs.unit.solid_phase.properties[0, 0]._params.mw_comp["Fe2O3"]
            )
        )
        stoichiometric_ratio = mole_solid_reacted / mole_gas_reacted
        assert pytest.approx(12, abs=1e-6) == stoichiometric_ratio

    @pytest.mark.ui
    @pytest.mark.unit
    def test_report(self, iron_oc):
        iron_oc.fs.unit.report()

    @pytest.mark.component
    def test_initialization_error(self, iron_oc):
        iron_oc.fs.unit.gas_outlet.flow_mol[0].fix(1)

        with pytest.raises(InitializationError):
            iron_oc.fs.unit.initialize()


# -----------------------------------------------------------------------------
class TestIronOC_TransformationMethod(object):
    @pytest.fixture(scope="class")
    def iron_oc(self):
        m = ConcreteModel()
        m.fs = FlowsheetBlock(dynamic=False)

        # Set up thermo props and reaction props
        m.fs.gas_properties = GasPhaseParameterBlock()
        m.fs.solid_properties = SolidPhaseParameterBlock()
        m.fs.hetero_reactions = HeteroReactionParameterBlock(
            solid_property_package=m.fs.solid_properties,
            gas_property_package=m.fs.gas_properties,
        )

        m.fs.unit = MBR(
            transformation_method="dae.collocation",
            gas_phase_config={"property_package": m.fs.gas_properties},
            solid_phase_config={
                "property_package": m.fs.solid_properties,
                "reaction_package": m.fs.hetero_reactions,
            },
        )

        # Fix geometry variables
        m.fs.unit.bed_diameter.fix(6.5)  # m
        m.fs.unit.bed_height.fix(5)  # m

        # Fix inlet port variables for gas and solid
        m.fs.unit.gas_inlet.flow_mol[0].fix(128.20513)  # mol/s
        m.fs.unit.gas_inlet.temperature[0].fix(1183.15)  # K
        m.fs.unit.gas_inlet.pressure[0].fix(2.00e5)  # Pa = 1E5 bar
        m.fs.unit.gas_inlet.mole_frac_comp[0, "CO2"].fix(0.02499)
        m.fs.unit.gas_inlet.mole_frac_comp[0, "H2O"].fix(0.00001)
        m.fs.unit.gas_inlet.mole_frac_comp[0, "CH4"].fix(0.975)

        m.fs.unit.solid_inlet.flow_mass[0].fix(591.4)  # kg/s
        m.fs.unit.solid_inlet.temperature[0].fix(1183.15)  # K
        m.fs.unit.solid_inlet.particle_porosity[0].fix(0.27)  # (-)
        m.fs.unit.solid_inlet.mass_frac_comp[0, "Fe2O3"].fix(0.45)
        m.fs.unit.solid_inlet.mass_frac_comp[0, "Fe3O4"].fix(1e-9)
        m.fs.unit.solid_inlet.mass_frac_comp[0, "Al2O3"].fix(0.55)

        return m

    @pytest.mark.build
    @pytest.mark.unit
    def test_build(self, iron_oc):
        assert hasattr(iron_oc.fs.unit, "gas_inlet")
        assert len(iron_oc.fs.unit.gas_inlet.vars) == 4
        assert isinstance(iron_oc.fs.unit.gas_inlet.flow_mol, Var)
        assert isinstance(iron_oc.fs.unit.gas_inlet.mole_frac_comp, Var)
        assert isinstance(iron_oc.fs.unit.gas_inlet.temperature, Var)
        assert isinstance(iron_oc.fs.unit.gas_inlet.pressure, Var)

        assert hasattr(iron_oc.fs.unit, "solid_inlet")
        assert len(iron_oc.fs.unit.solid_inlet.vars) == 4
        assert isinstance(iron_oc.fs.unit.solid_inlet.flow_mass, Var)
        assert isinstance(iron_oc.fs.unit.solid_inlet.particle_porosity, Var)
        assert isinstance(iron_oc.fs.unit.solid_inlet.mass_frac_comp, Var)
        assert isinstance(iron_oc.fs.unit.solid_inlet.temperature, Var)

        assert hasattr(iron_oc.fs.unit, "gas_outlet")
        assert len(iron_oc.fs.unit.gas_outlet.vars) == 4
        assert isinstance(iron_oc.fs.unit.gas_outlet.flow_mol, Var)
        assert isinstance(iron_oc.fs.unit.gas_outlet.mole_frac_comp, Var)
        assert isinstance(iron_oc.fs.unit.gas_outlet.temperature, Var)
        assert isinstance(iron_oc.fs.unit.gas_outlet.pressure, Var)

        assert hasattr(iron_oc.fs.unit, "solid_outlet")
        assert len(iron_oc.fs.unit.solid_outlet.vars) == 4
        assert isinstance(iron_oc.fs.unit.solid_outlet.flow_mass, Var)
        assert isinstance(iron_oc.fs.unit.solid_outlet.particle_porosity, Var)
        assert isinstance(iron_oc.fs.unit.solid_outlet.mass_frac_comp, Var)
        assert isinstance(iron_oc.fs.unit.solid_outlet.temperature, Var)

        assert number_variables(iron_oc) == 2279
        assert number_total_constraints(iron_oc) == 2241
        assert number_unused_variables(iron_oc) == 16
        print(unused_variables_set(iron_oc))

    @pytest.mark.unit
    def test_dof(self, iron_oc):
        assert degrees_of_freedom(iron_oc) == 0

    @pytest.fixture(scope="class")
    def iron_oc_unscaled(self, iron_oc):
        m = ConcreteModel()
        m.fs = FlowsheetBlock(dynamic=False)

        # Set up thermo props and reaction props
        m.fs.gas_properties = GasPhaseParameterBlock()
        m.fs.solid_properties = SolidPhaseParameterBlock()
        m.fs.hetero_reactions = HeteroReactionParameterBlock(
            solid_property_package=m.fs.solid_properties,
            gas_property_package=m.fs.gas_properties,
        )

        m.fs.unit = MBR(
            transformation_method="dae.collocation",
            gas_phase_config={"property_package": m.fs.gas_properties},
            solid_phase_config={
                "property_package": m.fs.solid_properties,
                "reaction_package": m.fs.hetero_reactions,
            },
        )

        # Fix geometry variables
        m.fs.unit.bed_diameter.fix(6.5)  # m
        m.fs.unit.bed_height.fix(5)  # m

        # Fix inlet port variables for gas and solid
        m.fs.unit.gas_inlet.flow_mol[0].fix(128.20513)  # mol/s
        m.fs.unit.gas_inlet.temperature[0].fix(1183.15)  # K
        m.fs.unit.gas_inlet.pressure[0].fix(2.00e5)  # Pa = 1E5 bar
        m.fs.unit.gas_inlet.mole_frac_comp[0, "CO2"].fix(0.02499)
        m.fs.unit.gas_inlet.mole_frac_comp[0, "H2O"].fix(0.00001)
        m.fs.unit.gas_inlet.mole_frac_comp[0, "CH4"].fix(0.975)

        m.fs.unit.solid_inlet.flow_mass[0].fix(591.4)  # kg/s
        m.fs.unit.solid_inlet.temperature[0].fix(1183.15)  # K
        m.fs.unit.solid_inlet.particle_porosity[0].fix(0.27)  # (-)
        m.fs.unit.solid_inlet.mass_frac_comp[0, "Fe2O3"].fix(0.45)
        m.fs.unit.solid_inlet.mass_frac_comp[0, "Fe3O4"].fix(1e-9)
        m.fs.unit.solid_inlet.mass_frac_comp[0, "Al2O3"].fix(0.55)

        return m

    @pytest.mark.solver
    @pytest.mark.skipif(solver is None, reason="Solver not available")
    @pytest.mark.component
    def test_initialize_unscaled(self, iron_oc_unscaled):
        initialization_tester(
            iron_oc_unscaled,
            optarg={"tol": 1e-5},
            gas_phase_state_args={
                "flow_mol": 128.20513,
                "temperature": 1183.15,
                "pressure": 2.00e5,
            },
            solid_phase_state_args={"flow_mass": 591.4, "temperature": 1183.15},
        )

    @pytest.mark.solver
    @pytest.mark.skipif(solver is None, reason="Solver not available")
    @pytest.mark.component
    def test_solve_unscaled(self, iron_oc_unscaled):
        solver.options = {"tol": 1e-5}
        results = solver.solve(iron_oc_unscaled)

        # Check for optimal solution
        assert check_optimal_termination(results)
        assert results.solver.status == SolverStatus.ok

    @pytest.mark.solver
    @pytest.mark.skipif(solver is None, reason="Solver not available")
    @pytest.mark.component
    def test_initialize(self, iron_oc):
        iscale.calculate_scaling_factors(iron_oc.fs.unit)
        initialization_tester(
            iron_oc,
            optarg={"tol": 1e-5},
            gas_phase_state_args={
                "flow_mol": 128.20513,
                "temperature": 1183.15,
                "pressure": 2.00e5,
            },
            solid_phase_state_args={"flow_mass": 591.4, "temperature": 1183.15},
        )

    @pytest.mark.solver
    @pytest.mark.skipif(solver is None, reason="Solver not available")
    @pytest.mark.component
    def test_solve_scaled(self, iron_oc):
        solver.options = {"tol": 1e-5}
        results = solver.solve(iron_oc)

        # Check for optimal solution
        assert check_optimal_termination(results)
        assert results.solver.status == SolverStatus.ok

    @pytest.mark.solver
    @pytest.mark.skipif(solver is None, reason="Solver not available")
    @pytest.mark.component
    def test_solution_scaled(self, iron_oc):
        assert (
            pytest.approx(0.1900, abs=1e-2)
            == iron_oc.fs.unit.velocity_superficial_gas[0, 0].value
        )
        assert (
            pytest.approx(0.5675, abs=1e-2)
            == iron_oc.fs.unit.velocity_superficial_gas[0, 1].value
        )
        assert (
            pytest.approx(0.0039, abs=1e-2)
            == iron_oc.fs.unit.velocity_superficial_solid[0].value
        )
        # Check the pressure drop that occurs across the bed
        assert (
            pytest.approx(198214.8255, abs=1e3)
            == iron_oc.fs.unit.gas_outlet.pressure[0].value
        )
        assert (
            pytest.approx(1785.1745, abs=1e2)
            == iron_oc.fs.unit.gas_inlet.pressure[0].value
            - iron_oc.fs.unit.gas_outlet.pressure[0].value
        )

    @pytest.mark.component
    def test_units_consistent(self, iron_oc):
        assert_units_consistent(iron_oc)

    @pytest.mark.solver
    @pytest.mark.skipif(solver is None, reason="Solver not available")
    @pytest.mark.component
    def test_conservation(self, iron_oc):
        # Conservation of material check
        calculate_variable_from_constraint(
            iron_oc.fs.unit.gas_phase.properties[0, 0].mw,
            iron_oc.fs.unit.gas_phase.properties[0, 0].mw_eqn,
        )
        calculate_variable_from_constraint(
            iron_oc.fs.unit.gas_phase.properties[0, 1].mw,
            iron_oc.fs.unit.gas_phase.properties[0, 1].mw_eqn,
        )
        mbal_gas = value(
            (
                iron_oc.fs.unit.gas_inlet.flow_mol[0]
                * iron_oc.fs.unit.gas_phase.properties[0, 0].mw
            )
            - (
                iron_oc.fs.unit.gas_outlet.flow_mol[0]
                * iron_oc.fs.unit.gas_phase.properties[0, 1].mw
            )
        )
        mbal_solid = value(
            iron_oc.fs.unit.solid_inlet.flow_mass[0]
            - iron_oc.fs.unit.solid_outlet.flow_mass[0]
        )
        mbal_tol = mbal_gas + mbal_solid
        assert abs(mbal_tol) <= 1e-2

        # Reaction stoichiometric ratio check
        # Overall reducer reactions for methane combustion:
        # CH4 + 12Fe2O3 => 8Fe3O4 + CO2 + 2H2O
        mole_gas_reacted = value(
            iron_oc.fs.unit.gas_inlet.flow_mol[0]
            * iron_oc.fs.unit.gas_inlet.mole_frac_comp[0, "CH4"]
            - iron_oc.fs.unit.gas_outlet.flow_mol[0]
            * iron_oc.fs.unit.gas_outlet.mole_frac_comp[0, "CH4"]
        )
        mole_solid_reacted = value(
            (
                iron_oc.fs.unit.solid_inlet.flow_mass[0]
                * iron_oc.fs.unit.solid_inlet.mass_frac_comp[0, "Fe2O3"]
                / iron_oc.fs.unit.solid_phase.properties[0, 1]._params.mw_comp["Fe2O3"]
            )
            - (
                iron_oc.fs.unit.solid_outlet.flow_mass[0]
                * iron_oc.fs.unit.solid_outlet.mass_frac_comp[0, "Fe2O3"]
                / iron_oc.fs.unit.solid_phase.properties[0, 0]._params.mw_comp["Fe2O3"]
            )
        )
        stoichiometric_ratio = mole_solid_reacted / mole_gas_reacted
        assert pytest.approx(12, abs=1e-6) == stoichiometric_ratio

    @pytest.mark.ui
    @pytest.mark.unit
    def test_report(self, iron_oc):
        iron_oc.fs.unit.report()

    @pytest.mark.component
    def test_initialization_error(self, iron_oc):
        iron_oc.fs.unit.gas_outlet.flow_mol[0].fix(1)

        with pytest.raises(InitializationError):
            iron_oc.fs.unit.initialize()


class TestBidirectionalSpatialDiscretization:
    @pytest.mark.unit
    def test_config_errors(self):
        m = ConcreteModel()
        m.fs = FlowsheetBlock(dynamic=False)
        m.fs.gas_properties = GasPhaseParameterBlock()
        m.fs.solid_properties = SolidPhaseParameterBlock()

        with pytest.raises(
            ConfigurationError,
            match="transformation_method must be",
        ):
            m.fs.unit1 = MBR(
                transformation_method="dae.collocation",
                gas_transformation_scheme="BACKWARD",
                gas_phase_config={"property_package": m.fs.gas_properties},
                solid_phase_config={"property_package": m.fs.solid_properties},
            )
            m.fs.unit1.build()

        with pytest.raises(
            ConfigurationError,
            match=r"Either both.*must be set",
        ):
            m.fs.unit2 = MBR(
                gas_transformation_scheme="BACKWARD",
                gas_phase_config={"property_package": m.fs.gas_properties},
                solid_phase_config={"property_package": m.fs.solid_properties},
            )
            m.fs.unit2.build()

        with pytest.raises(
            ConfigurationError,
            match="transformation_scheme cannot be specified",
        ):
            m.fs.unit3 = MBR(
                transformation_scheme="BACKWARD",
                gas_transformation_scheme="BACKWARD",
                solid_transformation_scheme="BACKWARD",
                gas_phase_config={"property_package": m.fs.gas_properties},
                solid_phase_config={"property_package": m.fs.solid_properties},
            )
            m.fs.unit3.build()

    @pytest.mark.unit
    def test_construct_dynamic(self):
        horizon = 300.0
        tfe_width = 100.0
        ntcp = 3

        nxfe = 3
        nxcp = 1

        time_set = [0.0, horizon]
        ntfe = round(horizon / tfe_width)

        model = ConcreteModel()
        model.fs = FlowsheetBlock(
            dynamic=True,
            time_set=time_set,
            time_units=pyo_units.s,
        )

        model.fs.gas_properties = GasPhaseParameterBlock()
        model.fs.solid_properties = SolidPhaseParameterBlock()
        model.fs.hetero_reactions = HeteroReactionParameterBlock(
            solid_property_package=model.fs.solid_properties,
            gas_property_package=model.fs.gas_properties,
        )

        model.fs.MB = MBR(
            finite_elements=nxfe,
            has_holdup=True,
            length_domain_set=[],
            transformation_method="dae.finite_difference",
            collocation_points=nxcp,
            gas_transformation_scheme="BACKWARD",
            solid_transformation_scheme="FORWARD",
            pressure_drop_type="ergun_correlation",
            gas_phase_config={"property_package": model.fs.gas_properties},
            solid_phase_config={
                "property_package": model.fs.solid_properties,
                "reaction_package": model.fs.hetero_reactions,
            },
        )

        time = model.fs.time
        discretizer = TransformationFactory("dae.collocation")
        discretizer.apply_to(
            model, wrt=time, nfe=ntfe, ncp=ntcp, scheme="LAGRANGE-RADAU"
        )

        gas_phase = model.fs.MB.gas_phase
        solid_phase = model.fs.MB.solid_phase

        gas_length = model.fs.MB.gas_phase.length_domain
        solid_length = model.fs.MB.solid_phase.length_domain
        bed_length = model.fs.MB.length_domain

        gas_components = model.fs.gas_properties.component_list
        solid_components = model.fs.solid_properties.component_list
        gas_phases = model.fs.gas_properties.phase_list
        solid_phases = model.fs.solid_properties.phase_list

        assert gas_length is not solid_length
        assert len(gas_length) == len(solid_length)
        assert len(gas_length) == len(bed_length)
        for x in gas_length:
            assert x in solid_length
            assert x in bed_length

    @pytest.mark.unit
    def test_var_indices(self):
        horizon = 300.0
        tfe_width = 100.0
        ntcp = 3

        nxfe = 3
        nxcp = 1

        time_set = [0.0, horizon]
        ntfe = round(horizon / tfe_width)

        model = ConcreteModel()
        model.fs = FlowsheetBlock(
            dynamic=True,
            time_set=time_set,
            time_units=pyo_units.s,
        )

        model.fs.gas_properties = GasPhaseParameterBlock()
        model.fs.solid_properties = SolidPhaseParameterBlock()
        model.fs.hetero_reactions = HeteroReactionParameterBlock(
            solid_property_package=model.fs.solid_properties,
            gas_property_package=model.fs.gas_properties,
        )

        model.fs.MB = MBR(
            finite_elements=nxfe,
            has_holdup=True,
            length_domain_set=[],
            transformation_method="dae.finite_difference",
            collocation_points=nxcp,
            gas_transformation_scheme="BACKWARD",
            solid_transformation_scheme="FORWARD",
            pressure_drop_type="ergun_correlation",
            gas_phase_config={"property_package": model.fs.gas_properties},
            solid_phase_config={
                "property_package": model.fs.solid_properties,
                "reaction_package": model.fs.hetero_reactions,
            },
        )

        time = model.fs.time
        discretizer = TransformationFactory("dae.collocation")
        discretizer.apply_to(
            model, wrt=time, nfe=ntfe, ncp=ntcp, scheme="LAGRANGE-RADAU"
        )

        gas_phase = model.fs.MB.gas_phase
        solid_phase = model.fs.MB.solid_phase

        gas_length = model.fs.MB.gas_phase.length_domain
        solid_length = model.fs.MB.solid_phase.length_domain
        bed_length = model.fs.MB.length_domain

        gas_components = model.fs.gas_properties.component_list
        solid_components = model.fs.solid_properties.component_list
        gas_phases = model.fs.gas_properties.phase_list
        solid_phases = model.fs.solid_properties.phase_list

        gas_disc_eqs = [
            gas_phase.material_flow_dx_disc_eq,
            gas_phase.enthalpy_flow_dx_disc_eq,
        ]
        gas_flow_vars = [gas_phase._flow_terms, gas_phase._enthalpy_flow]

        gas_disc_eqs = [
            dict(
                (idx, Reference(slice_))
                for idx, slice_ in slice_component_along_sets(
                    eq, (gas_length, solid_length)
                )
            )
            for eq in gas_disc_eqs
        ]
        gas_flow_vars = [
            dict(
                (idx, Reference(slice_))
                for idx, slice_ in slice_component_along_sets(
                    var, (gas_length, solid_length)
                )
            )
            for var in gas_flow_vars
        ]

        for eq_dict, var_dict in zip(gas_disc_eqs, gas_flow_vars):
            # NOTE: I am relying on fact that equations and variables
            # here have same non-space indices, and that they are in
            # the same order.
            for idx in eq_dict:
                for x in gas_length:
                    if x != gas_length.first():
                        x_prev = gas_length.prev(x)
                        eqdata = eq_dict[idx][x]
                        vardata = var_dict[idx][x]
                        vardata_prev = var_dict[idx][x_prev]
                        var_set = ComponentSet(identify_variables(eqdata.expr))
                        assert len(var_set) == 3
                        assert vardata in var_set
                        assert vardata_prev in var_set

        solid_disc_eqs = [
            solid_phase.material_flow_dx_disc_eq,
            solid_phase.enthalpy_flow_dx_disc_eq,
        ]
        solid_flow_vars = [
            solid_phase._flow_terms,
            solid_phase._enthalpy_flow,
        ]

        solid_disc_eqs = [
            dict(
                (idx, Reference(slice_))
                for idx, slice_ in slice_component_along_sets(
                    eq, (gas_length, solid_length)
                )
            )
            for eq in solid_disc_eqs
        ]
        solid_flow_vars = [
            dict(
                (idx, Reference(slice_))
                for idx, slice_ in slice_component_along_sets(
                    var, (gas_length, solid_length)
                )
            )
            for var in solid_flow_vars
        ]

        gas_disc_eqs = [
            gas_phase.material_flow_dx_disc_eq,
            gas_phase.enthalpy_flow_dx_disc_eq,
        ]
        gas_flow_vars = [
            gas_phase._flow_terms,
            gas_phase._enthalpy_flow,
        ]

        gas_disc_eqs = [
            dict(
                (idx, Reference(slice_))
                for idx, slice_ in slice_component_along_sets(
                    eq, (gas_length, solid_length)
                )
            )
            for eq in gas_disc_eqs
        ]
        gas_flow_vars = [
            dict(
                (idx, Reference(slice_))
                for idx, slice_ in slice_component_along_sets(
                    var, (gas_length, solid_length)
                )
            )
            for var in gas_flow_vars
        ]

        #
        # Test that discretization equations contain variables at the correct
        # indices
        #
        for eq_dict, var_dict in zip(gas_disc_eqs, gas_flow_vars):
            for idx in eq_dict:
                for x in gas_length:
                    if x != gas_length.first():
                        x_prev = gas_length.prev(x)
                        eqdata = eq_dict[idx][x]
                        vardata = var_dict[idx][x]
                        vardata_prev = var_dict[idx][x_prev]
                        var_set = ComponentSet(identify_variables(eqdata.expr))
                        assert len(var_set) == 3
                        assert vardata in var_set
                        assert vardata_prev in var_set

        solid_disc_eqs = [
            solid_phase.material_flow_dx_disc_eq,
            solid_phase.enthalpy_flow_dx_disc_eq,
        ]
        solid_flow_vars = [
            solid_phase._flow_terms,
            solid_phase._enthalpy_flow,
        ]

        solid_disc_eqs = [
            dict(
                (idx, Reference(slice_))
                for idx, slice_ in slice_component_along_sets(
                    eq, (gas_length, solid_length)
                )
            )
            for eq in solid_disc_eqs
        ]
        solid_flow_vars = [
            dict(
                (idx, Reference(slice_))
                for idx, slice_ in slice_component_along_sets(
                    var, (gas_length, solid_length)
                )
            )
            for var in solid_flow_vars
        ]

        #
        # Test that discretization equations contain variables at the correct
        # indices
        #
        for eq_dict, var_dict in zip(solid_disc_eqs, solid_flow_vars):
            for idx in eq_dict:
                for x in solid_length:
                    if x != solid_length.last():
                        x_prev = solid_length.next(x)
                        eqdata = eq_dict[idx][x]
                        vardata = var_dict[idx][x]
                        vardata_prev = var_dict[idx][x_prev]
                        var_set = ComponentSet(identify_variables(eqdata.expr))
                        assert len(var_set) == 3
                        assert vardata in var_set
                        assert vardata_prev in var_set
