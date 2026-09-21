"""Constants for the ADS integration tests."""

AMS_NET_ID = "192.168.1.10.1.1"
LOCAL_NET_ID = "192.168.1.20.1.1"
IP_ADDRESS = "192.168.1.10"
PORT = 851

# What the ADS library hands back for the notification on the device's own
# state: a notification handle, and no user handle, because subscribing to an
# address acquires no symbol handle to release later.
STATE_HANDLES: tuple[int, None] = (99, None)
