# New ONVIF Component for Home Assistant

Adding support for other PTZ move modes, presets, homing and reboot

- remove target_cameras set to all_cameras if entity_ids is empty ; change request addressed by @balloob in PR #29069
- add ContinuousMove, Stop and AbsoluteMove ptz commands in a service
- add advanced ptz service using "vector" for positioning and speed values
- add Reboot command in a service
- add Presets and Homing commands in a service
- add persistent notification to list existing presets positions stored in camera on service call
- add new config parameters
- refactor parts of the component to speed up service calls to ptz function(less http/Soap calls are made as recurrent needed profiles object are stored in class properties)
- move constants in a new const.py file
- execute Goke 7102 workaround (pr #26781) only if normal call fails first
- remove dead code- register services on the "onvif" DOMAIN ; comment addressed by @MartinHjelmare in PR 30152
- move services descrition in dedicated onvif/services.yaml ; comment addressed by @MartinHjelmare in PR 30152
- use dict[key] for required schema keys and keys with default schema values ; comment addressed by @MartinHjelmare in PR 30152

Sample config:

```
  - platform: onvif
    host: 192.168.1.x
    port: 80
    name: besder
    profile: 0 
    username: !secret besder_username
    rtsp_transport: "RTSP"
    continuous_timeout_compliance: False
```

New parameters explaination:

`rtsp_transport: "RTSP"` RTSP should fit most cases, also some cameras could support UDP or HTTP transport.

`continuous_timeout_compliance: False` Set it to False if your camera cannot handle embed Timeout in ContinousMove operation,  the component will force a Stop move operation after a sleep emulated timeout.
