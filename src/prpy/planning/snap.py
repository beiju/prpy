#!/usr/bin/env python

# Copyright (c) 2013, Carnegie Mellon University
# All rights reserved.
# Authors: Michael Koval <mkoval@cs.cmu.edu>
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# 
# - Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
# - Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
# - Neither the name of Carnegie Mellon University nor the names of its
#   contributors may be used to endorse or promote products derived from this
#   software without specific prior written permission.
# 
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
import logging, numpy, openravepy
from base import BasePlanner, PlanningError, UnsupportedPlanningError, PlanningMethod

class SnapPlanner(BasePlanner):
    def __init__(self):
        super(SnapPlanner, self).__init__()
 
    def __str__(self):
        return 'SnapPlanner'

    @PlanningMethod
    def PlanToConfiguration(self, robot, goal, snap_tolerance=0.1, **kw_args):
        """
        Attempt to plan a straight line trajectory from the robot's current
        configuration to the goal configuration. This will fail if the
        configurations differ by more than the tolerance.
        @param robot
        @param goal desired configuration
        @param snap_tolerance maximum Euclidean C-space distance in radians
        @return traj
        """
        return self._Snap(robot, goal, snap_tolerance=snap_tolerance, **kw_args)

    @PlanningMethod
    def PlanToEndEffectorPose(self, robot, goal_pose, snap_tolerance=0.1, **kw_args):
        """
        Attempt to plan a straight line trajectory from the robot's current
        configuration to a desired end-effector pose. This happens by finding
        the nearest IK solution to the robot's current configuration and
        attempts to snap there if possible.
        @param robot
        @param goal_pose desired end-effector pose
        @param snap_tolerance maximum Euclidean C-space distance in radians
        @return traj
        """
        # Find an IK solution.
        manipulator = robot.GetActiveManipulator()
        current_config = robot.GetDOFValues(manipulator.GetArmIndices())
        ik_param = openravepy.IkParameterization(goal_pose, openravepy.IkParameterizationType.Transform6D)
        ik_solutions = manipulator.FindIKSolutions(ik_param, openravepy.IkFilterOptions.CheckEnvCollisions)

        if ik_solutions.shape[0] == 0:
            raise PlanningError('There is no IK solution at the goal pose.')

        # Sort the IK solutions in ascending order by the costs returned by the
        # ranker. Lower cost solutions are better and infinite cost solutions are
        # assumed to be infeasible.
        scores = numpy.zeros(ik_solutions.shape[0])
        for i in xrange(scores.shape[0]):
            scores[i] = numpy.abs(ik_solutions[i] - current_config).max()

        sorted_indices = numpy.argsort(scores)
        sorted_indices = sorted_indices[~numpy.isposinf(scores)]
        sorted_ik_solutions = ik_solutions[sorted_indices, :]

        # Try snapping to the closest IK solution.
        return self._Snap(robot, sorted_ik_solutions[0, :], snap_tolerance=snap_tolerance, **kw_args)

    def _Snap(self, robot, goal, snap_tolerance, **kw_args):
        active_indices = robot.GetActiveDOFIndices()
        current_dof_values = robot.GetActiveDOFValues()

        # Only snap if we're close to the goal configuration.
        if (goal - current_dof_values).max() > snap_tolerance:
            raise PlanningError('Distance from goal larger than snap tolerance.')

        # Create a two-point trajectory that starts at our current
        # configuration and takes us to the goal.
        traj = openravepy.RaveCreateTrajectory(self.env, '')
        config_spec = robot.GetActiveConfigurationSpecification()
        active_indices = robot.GetActiveDOFIndices()

        waypoint1, waypoint2 = numpy.zeros(config_spec.GetDOF()), numpy.zeros(config_spec.GetDOF())
        config_spec.InsertJointValues(waypoint1, current_dof_values, robot, active_indices, False)
        config_spec.InsertJointValues(waypoint2, current_dof_values, robot, active_indices, False)

        traj.Init(config_spec)
        traj.Insert(0, waypoint1)
        traj.Insert(1, waypoint2)
        return traj