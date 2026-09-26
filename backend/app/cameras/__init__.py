"""Universal camera connectivity.

The rest of Visionary works with cameras, streams, capabilities and states. It never talks ONVIF, RTSP or a
vendor API directly: every connection method is a CameraConnection (app/cameras/connections.py) chosen by the
provider registry (app/cameras/providers.py).
"""
