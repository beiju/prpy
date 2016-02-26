import rospy
import chisel_msgs.srv
import chisel_msgs.msg
import math
import tf
import tf.transformations as transformations
import numpy
import os.path

'''
Typical way of running

from prpy.perception.chisel import ChiselModule
from offscreen_render import ros_camera_sim
robot.DetectObjects()
table = env.GetKinBody('table127')
glass = env.GetKinBody('plastic_glass124')
camera = ros_camera_sim.RosCameraSim(env)
camera.start('/head/kinect2/qhd')
camera.add_body(table)
camera.add_body(glass)

Nodelet manager screen
rosrun nodelet nodelet manager __name:=nodelet_manager
Depth converter screen
rosrun nodelet nodelet load depth_image_proc/convert_metric nodelet_manager image_raw:=/head/kinect2/qhd/image_depth_rect image:=/head/kinect2/qhd/image_depth_rect_float
In herb2, source herb2_chisel_ws/devel/setup.bash
Depth mask screen
roslaunch depth_mask launch_depth_mask.launch (args are correct)

Chisel screen
roslaunch chisel_ros launch_kinect_local.launch

IN PYTHON
mesh_client = openravepy.RaveCreateSensorSystem(env, 'mesh_marker')
chisel_det = ChiselModule(mesh_client,'Chisel')

robot.DetectObjects()
#reset chisel mesh
mesh_client.SendCommand('GetMesh Chisel/full_mesh')
'''



class ChiselModule():

	def __init__(self,mesh_client,service_namespace=None):

		try:
			rospy.init_node('chisel_detector',anonymous=True)
		except rospy.exceptions.ROSException:
			pass

		if service_namespace is None:
			service_namespace = 'Chisel'

		self.mesh_client = mesh_client
		self.serv_ns = service_namespace

	def DetectObject(self,robot,**kw_args):
    
		env = robot.GetEnv()
		already_in_env = False

		#Check if kinbody in env
		for body in env.GetBodies():
			if body.GetName() == 'Chisel/full_mesh':
				already_in_env = True
				break

		#Remove kinbody if present
		if already_in_env:
			chisel_kb = env.GetKinBody('Chisel/full_mesh')
			env.RemoveKinBody(chisel_kb)

		#Reset Chisel
		srv_nm = self.serv_ns+'/Reset'
		rospy.wait_for_service(srv_nm)
		detect_chisel_refresh = rospy.ServiceProxy(srv_nm,
	                                 chisel_msgs.srv.ResetService)

	`	#Get Kinbody and load into env
		#self.mesh_client.SendCommand('GetMesh Chisel/full_mesh')