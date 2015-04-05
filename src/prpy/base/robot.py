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

import functools, logging, openravepy, numpy
import prpy.util
from .. import bind, named_config, planning, util
from ..clone import Clone, Cloned
from ..tsr.tsrlibrary import TSRLibrary
from ..planning.base import Sequence 
from ..planning.ompl import OMPLSimplifier
from ..planning.retimer import ParabolicRetimer, ParabolicSmoother
from ..planning.mac_smoother import MacSmoother

logger = logging.getLogger('robot')

class Robot(openravepy.Robot):
    def __init__(self, robot_name=None):
        self.actions = None
        self.planner = None
        self.robot_name = robot_name

        try:
            self.tsrlibrary = TSRLibrary(self, robot_name=robot_name)
        except ValueError as e:
            self.tsrlibrary = None
            logger.warning('Failed creating TSRLibrary for robot "%s": %s',
                self.GetName(), e.message
            )

        self.controllers = list()
        self.manipulators = list()
        self.configurations = named_config.ConfigurationLibrary()
        self.multicontroller = openravepy.RaveCreateMultiController(self.GetEnv(), '')
        self.SetController(self.multicontroller)

        # Standard, commonly-used OpenRAVE plugins.
        self.base_manipulation = openravepy.interfaces.BaseManipulation(self)
        self.task_manipulation = openravepy.interfaces.TaskManipulation(self)

        # Path post-processing for execution. This includes simplification of
        # the geometric path, retiming a path into a trajectory, and smoothing
        # (joint simplificaiton and retiming).
        self.simplifier = OMPLSimplifier()
        self.retimer = ParabolicRetimer()
        self.smoother = Sequence(
            ParabolicSmoother(),
            self.retimer
        )

    def __dir__(self):
        # We have to manually perform a lookup in InstanceDeduplicator because
        # __methods__ bypass __getattribute__.
        self = bind.InstanceDeduplicator.get_canonical(self)

        # Add planning and action methods to the tab-completion list.
        method_names = set(self.__dict__.keys())

        if hasattr(self, 'planner') and self.planner is not None:
            method_names.update(self.planner.get_planning_method_names())
        if hasattr(self, 'actions') and self.actions is not None:
            method_names.update(self.actions.get_actions())

        return list(method_names)

    def __getattr__(self, name):
        # We have to manually perform a lookup in InstanceDeduplicator because
        # __methods__ bypass __getattribute__.
        self = bind.InstanceDeduplicator.get_canonical(self)

        if (hasattr(self, 'planner') and self.planner is not None
            and self.planner.has_planning_method(name)):

            delegate_method = getattr(self.planner, name)
            @functools.wraps(delegate_method)
            def wrapper_method(*args, **kw_args):
                return self._PlanWrapper(delegate_method, args, kw_args)

            return wrapper_method
        elif (hasattr(self, 'actions') and self.actions is not None
              and self.actions.has_action(name)):

            delegate_method = self.actions.get_action(name)
            @functools.wraps(delegate_method)
            def wrapper_method(obj, *args, **kw_args):
                return delegate_method(self, obj, *args, **kw_args)
            return wrapper_method

        raise AttributeError('{0:s} is missing method "{1:s}".'.format(repr(self), name))

    def CloneBindings(self, parent):
        self.planner = parent.planner

        # TODO: This is a bit of a mess. We need to clean this up when we
        # finish the smoothing refactor.
        self.simplifier = parent.simplifier
        self.retimer = parent.retimer
        self.smoother = parent.smoother

        self.robot_name = parent.robot_name
        self.tsrlibrary = parent.tsrlibrary
        self.configurations = parent.configurations

        self.controllers = []
        self.SetController(None)

        self.manipulators = [Cloned(manipulator, into=self.GetEnv())
                             for manipulator in parent.manipulators]

        # TODO: Do we really need this in cloned environments?
        self.base_manipulation = openravepy.interfaces.BaseManipulation(self)
        self.task_manipulation = openravepy.interfaces.TaskManipulation(self)

    def AttachController(self, name, args, dof_indices, affine_dofs, simulated):
        """
        Create and attach a controller to a subset of this robot's DOFs. If
        simulated is False, a controller is created using 'args' and is attached
        to the multicontroller. In simulation mode an IdealController is
        created instead. Regardless of the simulation mode, the multicontroller
        must be finalized before use.  @param name user-readable name used to identify this controller
        @param args real controller arguments
        @param dof_indices controlled joint DOFs
        @param affine_dofs controleld affine DOFs
        @param simulated simulation mode
        @returns created controller
        """
        if simulated:
            args = 'IdealController'

        delegate_controller = openravepy.RaveCreateController(self.GetEnv(), args)
        if delegate_controller is None:
            type_name = args.split()[0]
            message = 'Creating controller {0:s} of type {1:s} failed.'.format(name, type_name)
            raise openravepy.openrave_exception(message)

        self.multicontroller.AttachController(delegate_controller, dof_indices, affine_dofs)

        return delegate_controller

    def GetTrajectoryManipulators(self, traj):
        """
        Extract the manipulators that are active in a trajectory. A manipulator
        is considered active if joint values are specified for one or more of its
        controlled DOFs.
        @param traj input trajectory
        @returns list of active manipulators
        """
        traj_indices = set(util.GetTrajectoryIndices(traj))

        active_manipulators = []
        for manipulator in self.GetManipulators():
            manipulator_indices = set(manipulator.GetArmIndices())
            if traj_indices & manipulator_indices:
                active_manipulators.append(manipulator)

        return active_manipulators

    def PostProcessPath(self, path, defer=False, executor=None,
                        constrained=None, smooth=None, default_timelimit=0.5,
                        shortcut_options=None, smoothing_options=None,
                        retiming_options=None):
        """ Post-process a geometric path to prepare it for execution.

        This method post-processes a geometric path by (optionally) optimizing
        it and timing it. Three different post-processing pipelines are used:

        1. For constrained trajectories, we do not modify the geometric path
           and retime the path to be time-optimal. This trajectory must stop
           at every waypoint. The only exception is for...
        2. For smooth trajectories, we attempt to fit a time-optimal smooth
           curve through the waypoints (not implemented). If this curve is
           not collision free, then we fall back on...
        3. By default, we run a smoother that jointly times and smooths the
           path. This algorithm can change the geometric path to optimize
           runtime.

        The behavior in (1) and (2) can be forced by passing constrained=True
        or smooth=True. By default, the case is inferred by the tag(s) attached
        to the trajectory: (1) is triggered by the CONSTRAINED tag and (2) is
        tiggered by the SMOOTH tag.

        Options an be passed to each post-processing routine using the
        shortcut-options, smoothing_options, and retiming_options **kwargs
        dictionaries. If no "timelimit" is specified in any of these
        dictionaries, it defaults to default_timelimit seconds.

        @param path un-timed OpenRAVE trajectory
        @param defer return immediately with a future trajectory
        @param executor executor to use when defer = True
        @param constrained the path is constrained; do not change it
        @param smooth the path is smooth; attempt to execute it directly
        @param default_timelimit timelimit for all operations, if not set
        @param shortcut_options kwargs to ShortcutPath for shortcutting
        @param smoothing_options kwargs to RetimeTrajectory for smoothing
        @param retiming_options kwargs to RetimeTrajectory for timing
        @return trajectory ready for execution
        """
        from ..planning.base import Tags
        from ..util import GetTrajectoryTags, CopyTrajectory

        # Default parameters.
        if shortcut_options is None:
            shortcut_options = dict()
        if smoothing_options is None:
            smoothing_options = dict()
        if retiming_options is None:
            retiming_options = dict()

        shortcut_options.setdefault('timelimit', default_timelimit)
        smoothing_options.setdefault('timelimit', default_timelimit)
        retiming_options.setdefault('timelimit', default_timelimit)

        # Read default parameters from the trajectory's tags.
        tags = GetTrajectoryTags(path)

        if constrained is None:
            constrained = tags.get(Tags.CONSTRAINED, False)
            logger.debug('Detected "%s" tag on trajectory: Setting'
                         ' constrained = True.', Tags.CONSTRAINED)

        if smooth is None:
            smooth = tags.get(Tags.SMOOTH, False)
            logger.debug('Detected "%s" tag on trajectory: Setting smooth'
                         ' = True', Tags.SMOOTH)

        def do_postprocess():
            with Clone(self.GetEnv()) as cloned_env:
                cloned_robot = cloned_env.Cloned(self)

                # Planners only operate on the active DOFs. We'll set any DOFs
                # in the trajectory as active.
                env = path.GetEnv()
                cspec = path.GetConfigurationSpecification()
                used_bodies = cspec.ExtractUsedBodies(env)
                if self not in used_bodies:
                    raise ValueError(
                        'Robot "{:s}" is not in the trajectory.'.format(
                            self.GetName()))

                dof_indices, _ = cspec.ExtractUsedIndices(self)
                cloned_robot.SetActiveDOFs(dof_indices)
                logger.debug(
                    'Setting robot "%s" DOFs %s as active for post-processing.',
                    cloned_robot.GetName(), list(dof_indices))

                # TODO: Handle a affine DOF trajectories for the base.

                # Directly compute a timing of smooth trajectories.
                if smooth:
                    logger.warning(
                        'Post-processing smooth paths is not supported.'
                        ' Using the default post-processing logic; this may'
                        ' significantly change the geometric path.'
                    )

                # The trajectory is constrained. Retime it without changing the
                # geometric path.
                if constrained:
                    logger.debug('Retiming a constrained path. The output'
                                 ' trajectory will stop at every waypoint.')
                    traj = self.retimer.RetimeTrajectory(
                        cloned_robot, path, defer=False, **retiming_options)
                else:
                # The trajectory is not constrained, so we can shortcut it
                # before execution.
                    logger.debug('Shortcutting an unconstrained path.')
                    shortcut_path = self.simplifier.ShortcutPath(
                        cloned_robot, path, defer=False, **shortcut_options)

                    logger.debug('Smoothing an unconstrained path.')
                    traj = self.smoother.RetimeTrajectory(
                        cloned_robot, shortcut_path, defer=False,
                        **smoothing_options)

                return CopyTrajectory(traj, env=self.GetEnv())

        if defer:
            from trollius.executor import get_default_executor
            from trollius.futures import wrap_future

            if executor is None:
                executor = get_default_executor()

            return wrap_future(executor.submit(do_postprocess))
        else:
            return do_postprocess()

    def ExecutePath(self, path, defer=False, executor=None, **kwargs):
        """ Post-process and execute an un-timed path.

        This method calls PostProcessPath, then passes the result to
        ExecuteTrajectory. Any extra **kwargs are forwarded to both of these
        methods. This function returns the timed trajectory that was executed
        on the robot.

        @param path OpenRAVE trajectory representing an un-timed path
        @param defer execute asynchronously and return a future
        @param executor if defer = True, which executor to use
        @param **kwargs forwarded to PostProcessPath and ExecuteTrajectory
        @return timed trajectory executed on the robot
        """

        def do_execute():
            logger.debug('Post-processing path to compute a timed trajectory.')
            traj = self.PostProcessPath(path, defer=False, **kwargs)

            logger.debug('Executing timed trajectory.')
            return self.ExecuteTrajectory(traj, defer=False, **kwargs)

        if defer:
            from trollius.executor import get_default_executor
            from trollius.futures import wrap_future

            if executor is None:
                executor = get_default_executor()

            return wrap_future(executor.submit(do_execute))
        else:
            return do_execute()

    def ExecuteTrajectory(self, traj, defer=False, timeout=None, period=0.01):
        """ Executes a time trajectory on the robot.

        This function directly executes a timed OpenRAVE trajectory on the
        robot. If you have a geometric path, such as those returned by a
        geometric motion planner, you should first time the path using
        PostProcessPath. Alternatively, you could use the ExecutePath helper
        function to time and execute the path in one function call.

        If timeout = None (the default), this function does not return until
        execution has finished. Termination occurs if the trajectory is
        successfully executed or if a fault occurs (in this case, an exception
        will be raised). If timeout is a float (including timeout = 0), this
        function will return None once the timeout has ellapsed, even if the
        trajectory is still being executed.
        
        NOTE: We suggest that you either use timeout=None or defer=True. If
        trajectory execution times out, there is no way to tell whether
        execution was successful or not. Other values of timeout are only
        supported for legacy reasons.

        This function returns the trajectory that was actually executed on the
        robot, including controller error. If this is not available, the input
        trajectory will be returned instead.

        @param traj timed OpenRAVE trajectory to be executed
        @param defer execute asynchronously and return a trajectory Future
        @param timeout maximum time to wait for execution to finish
        @param period poll rate, in seconds, for checking trajectory status
        @return trajectory executed on the robot
        """

        # TODO: Verify that the trajectory is timed.
        # TODO: Check if this trajectory contains the base.

        needs_base = util.HasAffineDOFs(traj.GetConfigurationSpecification())

        self.GetController().SetPath(traj)

        active_manipulators = self.GetTrajectoryManipulators(traj)
        active_controllers = [
            active_manipulator.controller \
            for active_manipulator in active_manipulators \
            if hasattr(active_manipulator, 'controller')
        ]

        if needs_base:
            if (hasattr(self, 'base') and hasattr(self.base, 'controller')
                    and self.base.controller is not None):
                active_controllers.append(self.base.controller)
            else:
                logger.warning(
                    'Trajectory includes the base, but no base controller is'
                    ' available. Is self.base.controller set?')

        if defer:
            import time
            import trollius

            @trollius.coroutine
            def do_poll():
                time_stop = time.time() + (timeout if timeout else numpy.inf)

                while time.time() <= time_stop:
                    is_done = all(controller.IsDone()
                                  for controller in active_controllers)
                    if is_done:
                        raise trollius.Return(traj)

                    yield trollius.From(trollius.sleep(period))

                raise trollius.Return(None)

            return trollius.async(do_poll())
        else:
            util.WaitForControllers(active_controllers, timeout=timeout)

        return traj

    def ViolatesVelocityLimits(self, traj):
        """
        Checks a trajectory for velocity limit violations
        @param traj input trajectory
        """
        # Get the limits that pertain to this trajectory
        all_velocity_limits = self.GetDOFVelocityLimits()
        traj_indices = util.GetTrajectoryIndices(traj)
        velocity_limits = [all_velocity_limits[idx] for idx in traj_indices]

        # Get the velocity group from the configuration specification so
        #  that we know the offset and number of dofs
        config_spec = traj.GetConfigurationSpecification()
        num_waypoints = traj.GetNumWaypoints()

        # Check for the velocity group
        has_velocity_group = True
        try:
            config_spec.GetGroupFromName('joint_velocities')
        except openravepy.openrave_exception:
            logging.warn('Trajectory does not have joint velocities defined')
            has_velocity_group = False

        # Now check all the waypoints
        for idx in range(0, num_waypoints):

            wpt = traj.GetWaypoint(idx)
            
            if has_velocity_group:
                # First check the velocities defined for the waypoint
                velocities = config_spec.ExtractJointValues(wpt, self, traj_indices, 1)
                for vidx in range(len(velocities)):
                    if (velocities[vidx] > velocity_limits[vidx]):
                        logging.warn('Velocity for waypoint %d joint %d violates limits (value: %0.3f, limit: %0.3f)' % 
                                     (idx, vidx, velocities[vidx], velocity_limits[vidx]))
                        return True

            # Now check the velocities calculated by differencing positions
            dt = config_spec.ExtractDeltaTime(wpt)
            values = config_spec.ExtractJointValues(wpt, self, traj_indices, 0)

            if idx > 0:
                diff_velocities = numpy.fabs(values - prev_values)/(dt - prev_dt)
                for vidx in range(len(diff_velocities)):
                    if (diff_velocities[vidx] > velocity_limits[vidx]):
                        logging.warn('Velocity for waypoint %d joint %d violates limits (value: %0.3f, limit: %0.3f)' % 
                                     (idx, vidx, diff_velocities[vidx], velocity_limits[vidx]))
                        return True

            # Set current to previous
            prev_dt = dt
            prev_values = values

        return False

    def _PlanWrapper(self, planning_method, args, kw_args):
        config_spec = self.GetActiveConfigurationSpecification('linear')

        # Call the planner.
        result = planning_method(self, *args, **kw_args)

        def postprocess_trajectory(traj):
            # Strip inactive DOFs from the trajectory.
            openravepy.planningutils.ConvertTrajectorySpecification(
                traj, config_spec
            )

        # Return either the trajectory result or a future to the result.
        if kw_args.get('defer', False):
            import trollius

            # Perform postprocessing on a future trajectory.
            @trollius.coroutine
            def defer_trajectory(traj_future, kw_args):
                # Wait for the planner to complete.
                traj = yield trollius.From(traj_future)

                postprocess_trajectory(traj)

                # Optionally execute the trajectory.
                if kw_args.get('execute', True):
                    # We know defer = True if we're in this function, so we
                    # don't have to set it explicitly.
                    traj = yield trollius.From(
                        self.ExecutePath(traj, **kw_args)
                    )

                raise trollius.Return(traj)

            return trollius.Task(defer_trajectory(result, kw_args))
        else:
            postprocess_trajectory(result)

            # Optionally execute the trajectory.
            if kw_args.get('execute', True):
                result = self.ExecutePath(result, **kw_args)

            return result
