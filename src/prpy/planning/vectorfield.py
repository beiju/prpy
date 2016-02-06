#!/usr/bin/env python

# Copyright (c) 2015, Carnegie Mellon University
# All rights reserved.
# Authors: Siddhartha Srinivasa <siddh@cs.cmu.edu>
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

import logging
import numpy
import openravepy
import time
from .. import util
from base import BasePlanner, PlanningError, PlanningMethod, Tags
from enum import Enum
import math

logger = logging.getLogger(__name__)


class TerminationError(PlanningError):
    def __init__(self):
        super(TerminationError, self).__init__('Terminated by callback.')


class TimeLimitError(PlanningError):
    def __init__(self):
        super(TimeLimitError, self).__init__('Reached time limit.')


class Status(Enum):
    '''
    CONTINUE - keep going
    TERMINATE - stop gracefully and output the CACHEd trajectory
    CACHE_AND_CONTINUE - save the current trajectory and CONTINUE.
                         return the saved trajectory if TERMINATEd.
    CACHE_AND_TERMINATE - save the current trajectory and TERMINATE
    '''
    TERMINATE = -1
    CACHE_AND_CONTINUE = 0
    CONTINUE = 1
    CACHE_AND_TERMINATE = 2

    @classmethod
    def DoesTerminate(cls, status):
        return status in [cls.TERMINATE, cls.CACHE_AND_TERMINATE]

    @classmethod
    def DoesCache(cls, status):
        return status in [cls.CACHE_AND_CONTINUE, cls.CACHE_AND_TERMINATE]


class VectorFieldPlanner(BasePlanner):
    def __init__(self):
        super(VectorFieldPlanner, self).__init__()

    def __str__(self):
        return 'VectorFieldPlanner'

    @PlanningMethod
    def PlanToEndEffectorPose(self, robot, goal_pose, timelimit=5.0,
                              pose_error_tol=0.01, **kw_args):
        """
        Plan to an end effector pose by following a geodesic loss function
        in SE(3) via an optimized Jacobian.

        @param robot
        @param goal_pose desired end-effector pose
        @param timelimit time limit before giving up
        @param pose_error_tol in meters
        @return traj
        """
        manip = robot.GetActiveManipulator()

        def vf_geodesic():
            twist = util.GeodesicTwist(manip.GetEndEffectorTransform(),
                                            goal_pose)
            dqout, tout = util.ComputeJointVelocityFromTwist(
                robot, twist, joint_velocity_limits=numpy.PINF)

            # Go as fast as possible
            vlimits = robot.GetDOFVelocityLimits(robot.GetActiveDOFIndices())
            return min(abs(vlimits[i] / dqout[i]) if dqout[i] != 0. else 1. for i in xrange(vlimits.shape[0])) * dqout

        def CloseEnough():
            pose_error = util.GeodesicDistance(
                        manip.GetEndEffectorTransform(),
                        goal_pose)
            if pose_error < pose_error_tol:
                return Status.TERMINATE
            return Status.CONTINUE

        traj = self.FollowVectorField(robot, vf_geodesic, CloseEnough,
                                      timelimit)

        # Flag this trajectory as unconstrained. This overwrites the
        # constrained flag set by FollowVectorField.
        util.SetTrajectoryTags(traj, {Tags.CONSTRAINED: False}, append=True)
        return traj

    @PlanningMethod
    def PlanToEndEffectorOffset(self, robot, direction, distance,
                                max_distance=None, timelimit=5.0,
                                position_tolerance=0.01,
                                angular_tolerance=0.15,
                                **kw_args):
        """
        Plan to a desired end-effector offset with move-hand-straight
        constraint. movement less than distance will return failure. The motion
        will not move further than max_distance.
        @param robot
        @param direction unit vector in the direction of motion
        @param distance minimum distance in meters
        @param max_distance maximum distance in meters
        @param timelimit timeout in seconds
        @param position_tolerance constraint tolerance in meters
        @param angular_tolerance constraint tolerance in radians
        @return traj
        """
        if distance < 0:
            raise ValueError('Distance must be non-negative.')
        elif numpy.linalg.norm(direction) == 0:
            raise ValueError('Direction must be non-zero')
        elif max_distance is not None and max_distance < distance:
            raise ValueError('Max distance is less than minimum distance.')
        elif position_tolerance < 0:
            raise ValueError('Position tolerance must be non-negative.')
        elif angular_tolerance < 0:
            raise ValueError('Angular tolerance must be non-negative.')

        # Normalize the direction vector.
        direction = numpy.array(direction, dtype='float')
        direction /= numpy.linalg.norm(direction)

        manip = robot.GetActiveManipulator()
        Tstart = manip.GetEndEffectorTransform()

        def vf_straightline():
            twist = util.GeodesicTwist(manip.GetEndEffectorTransform(),
                                            Tstart)
            twist[0:3] = direction

            dqout, _ = util.ComputeJointVelocityFromTwist(
                robot, twist, joint_velocity_limits=numpy.PINF)

            return dqout

        def TerminateMove():
            '''
            Fail if deviation larger than position and angular tolerance.
            Succeed if distance moved is larger than max_distance.
            Cache and continue if distance moved is larger than distance.
            '''
            from .exceptions import ConstraintViolationPlanningError 

            Tnow = manip.GetEndEffectorTransform()
            error = util.GeodesicError(Tstart, Tnow)
            if numpy.fabs(error[3]) > angular_tolerance:
                raise ConstraintViolationPlanningError(
                    'Deviated from orientation constraint.')
            distance_moved = numpy.dot(error[0:3], direction)
            position_deviation = numpy.linalg.norm(error[0:3] -
                                                   distance_moved*direction)
            if position_deviation > position_tolerance:
                raise ConstraintViolationPlanningError(
                    'Deviated from straight line constraint.')

            if max_distance is None:
                if distance_moved > distance:
                    return Status.CACHE_AND_TERMINATE
            elif distance_moved > max_distance:
                return Status.TERMINATE
            elif distance_moved >= distance:
                return Status.CACHE_AND_CONTINUE

            return Status.CONTINUE

        return self.FollowVectorField(robot, vf_straightline, TerminateMove,
                                      timelimit, **kw_args)

    @PlanningMethod
    def PlanWorkspacePath(self, robot, traj, timelimit=5.0):
        """
        Follow a workspace trajectory
        
        @param robot
        @param traj workspace traj
                    represented as OpenRAVE Affine Trajectory 
                    TODO: double check if this is what I truly mean
        @param timelimit time limit before giving up
        TODO do i need other parameters like other planning methods
        """
        manip = robot.GetActiveManipulator()

        def FollowNextPoint():
            curr_location = manip.GetEndEffectorTransform()
            
            # run one-way hausdorff distance to find the point in the 
            # reference trajectory that is closest to the current location 
            reference_location = numpy.eye(4) #TODO replace this later
            perpendicular_error = util.GeodesicTwist(curr_location, reference_location)
            kp = numpy.eye(6)

            #TODO tune to something reasonable
            offset = numpy.array([1, 1, 1, 1, 1, 1])

            #Get velocity of reference traj at the reference_location
            #Use JointStateFromTraj(robot, reference_traj, (time? workspace path is untimed..?), derivatives)
                #might have to write my own for this that does look up
            #velocity_parallel = 

            #return velocity_parallel + (kp*offset)


        def CompletedTraj():
            #TODO what is the end condition?
            #Could get closest point (in reference traj wrt one-way hausdorff)
            #if the geodesicDistance is is within some tolerable pose error 
            #then terminate..?

        traj = self.FollowVectorField(robot, FollowNextPoint, CompletedTraj, timelimit)
        return traj

    @PlanningMethod
    def FollowVectorField(self, robot, fn_vectorfield, fn_terminate,
                          integration_timelimit=10.,
                          timelimit=5.0, dt_multiplier=1.01, **kw_args):
        """
        Follow a joint space vectorfield to termination.

        @param robot
        @param fn_vectorfield a vectorfield of joint velocities
        @param fn_terminate custom termination condition
        @param timelimit time limit before giving up
        @param dt_multiplier multiplier of the minimum resolution at which
               the vector field will be followed. Defaults to 1.0.
               Any larger value means the vectorfield will be re-evaluated
               floor(dt_multiplier) steps
        @param kw_args keyword arguments to be passed to fn_vectorfield
        @return traj
        """
        from .exceptions import (
            CollisionPlanningError,
            SelfCollisionPlanningError,
            TimeoutPlanningError,
            JointLimitError
        )
        from openravepy import CollisionReport, RaveCreateTrajectory
        from ..util import ComputeJointVelocityFromTwist, GetCollisionCheckPts, ComputeUnitTiming
        import time
        import scipy.integrate

        CheckLimitsAction = openravepy.KinBody.CheckLimitsAction

        # This is a workaround to emulate 'nonlocal' in Python 2.
        nonlocals = {
            'exception': None,
            't_cache': None,
            't_check': 0.,
        }

        env = robot.GetEnv()
        active_indices = robot.GetActiveDOFIndices()
        q_limit_min, q_limit_max = robot.GetActiveDOFLimits()
        qdot_limit = robot.GetDOFVelocityLimits(active_indices)

        cspec = robot.GetActiveConfigurationSpecification('linear')
        cspec.AddDeltaTimeGroup()
        cspec.ResetGroupOffsets()

        path = RaveCreateTrajectory(env, '')
        path.Init(cspec)

        time_start = time.time()

        def fn_wrapper(t, q):
            robot.SetActiveDOFValues(q, CheckLimitsAction.Nothing)
            return fn_vectorfield()

        def fn_status_callback(t, q):
            if time.time() - time_start >= timelimit:
                raise TimeLimitError()

            # Check joint position limits. Do this before setting the DOF so we
            # don't set the DOFs out of limits.
            lower_position_violations = (q < q_limit_min)
            if lower_position_violations.any():
                index = lower_position_violations.nonzero()[0][0]
                raise JointLimitError(robot,
                    dof_index=active_indices[index],
                    dof_value=q[index],
                    dof_limit=q_limit_min[index],
                    description='position')

            upper_position_violations = (q> q_limit_max)
            if upper_position_violations.any():
                index = upper_position_violations.nonzero()[0][0]
                raise JointLimitError(robot,
                    dof_index=active_indices[index],
                    dof_value=q[index],
                    dof_limit=q_limit_max[index],
                    description='position')

            robot.SetActiveDOFValues(q)

            # Check collision.
            report = CollisionReport()
            if env.CheckCollision(robot, report=report):
                raise CollisionPlanningError.FromReport(report)
            elif robot.CheckSelfCollision(report=report):
                raise SelfCollisionPlanningError.FromReport(report)

            # Check the termination condition.
            status = fn_terminate()

            if Status.DoesCache(status):
                nonlocals['t_cache'] = t

            if Status.DoesTerminate(status):
                raise TerminationError()

        def fn_callback(t, q):
            try:
                # Add the waypoint to the trajectory.
                waypoint = numpy.zeros(cspec.GetDOF())
                cspec.InsertDeltaTime(waypoint, t - path.GetDuration())
                cspec.InsertJointValues(waypoint, q, robot, active_indices, 0)
                path.Insert(path.GetNumWaypoints(), waypoint)

                # Run constraint checks at DOF resolution.
                if path.GetNumWaypoints() == 1:
                    checks = [(t, q)]
                else:
                    # TODO: This should start at t_check. Unfortunately, a bug
                    # in GetCollisionCheckPts causes this to enter an infinite
                    # loop.
                    checks = GetCollisionCheckPts(robot, path,
                        include_start=False) #start_time=nonlocals['t_check'])

                for t_check, q_check in checks:
                    fn_status_callback(t_check, q_check)

                    # Record the time of this check so we continue checking at
                    # DOF resolution the next time the integrator takes a step.
                    nonlocals['t_check'] = t_check

                return 0 # Keep going.
            except PlanningError as e:
                nonlocals['exception'] = e
                return -1 # Stop.

        # Integrate the vector field to get a configuration space path.
        # TODO: Tune the integrator parameters.
        integrator = scipy.integrate.ode(f=fn_wrapper)
        integrator.set_integrator(name='dopri5',
            first_step=0.1, atol=1e-3, rtol=1e-3)
        integrator.set_solout(fn_callback)
        integrator.set_initial_value(y=robot.GetActiveDOFValues(), t=0.)
        integrator.integrate(t=integration_timelimit)

        t_cache = nonlocals['t_cache']
        exception = nonlocals['exception'] 

        if t_cache is None:
            raise exception or PlanningError('An unknown error has occurred.')
        elif exception:
            logger.warning('Terminated early: %s', str(exception))

        # Remove any parts of the trajectory that are not cached. This also
        # strips the (potentially infeasible) timing information.
        output_cspec = robot.GetActiveConfigurationSpecification('linear')
        output_path = RaveCreateTrajectory(env, '')
        output_path.Init(output_cspec)

        # Add all waypoints before the last integration step. GetWaypoints does
        # not include the upper bound, so this is safe.
        cached_index = path.GetFirstWaypointIndexAfterTime(t_cache)
        output_path.Insert(0, path.GetWaypoints(0, cached_index), cspec)

        # Add a segment for the feasible part of the last integration step.
        output_path.Insert(output_path.GetNumWaypoints(),
            path.Sample(t_cache), cspec)

        # Flag this trajectory as constrained.
        util.SetTrajectoryTags(
            output_path, {
                Tags.CONSTRAINED: 'true',
                Tags.SMOOTH: 'true'
            }, append=True
        )
        return output_path

