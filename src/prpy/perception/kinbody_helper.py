
def transform_to_or(kinbody, detection_frame, destination_frame, 
                    reference_link=None):
    """
    Transform the pose of a kinbody from a pose determined using TF to 
    the correct relative pose in OpenRAVE. This transformation is performed
    by providing a link in OpenRAVE that corresponds directly to a frame in TF.

    @param kinbody The kinbody to transform    
    @param detection_frame The tf frame the kinbody was originally detected in
    @param destination_frame A tf frame that has a direct correspondence with
      a link on an OpenRAVE Kinbody
    @param reference_link The OpenRAVE link that corresponds to the tf frame
      given by the destination_frame parameter (if None it is assumed
      that the transform between the OpenRAVE world and the destination_frame 
      tf frame is the identity)
    """

    import numpy, tf, rospy
    listener = tf.TransformListener()

    listener.waitForTransform(
        detection_frame, destination_frame,
        rospy.Time(),
        rospy.Duration(10))

    frame_trans, frame_rot = listener.lookupTransform(
        destination_frame, detection_frame,
        rospy.Time(0))
        
    from tf.transformations import quaternion_matrix
    detection_in_destination = numpy.array(numpy.matrix(quaternion_matrix(frame_rot)))
    detection_in_destination[:3,3] = frame_trans
    
    body_in_destination = numpy.dot(detection_in_destination, kinbody.GetTransform())

    if reference_link is not None:
        destination_in_or = reference_link.GetTransform()
    else:        
        destination_in_or = numpy.eye(4)

    body_in_or= numpy.dot(destination_in_or, body_in_destination)
    kinbody.SetTransform(body_in_or)
