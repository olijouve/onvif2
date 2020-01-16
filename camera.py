"""
Support for ONVIF Cameras with FFmpeg as decoder.

For more details about this platform, please refer to the documentation at
https://home-assistant.io/components/camera.onvif/
"""
import asyncio
import datetime as dt
import logging
import os

from aiohttp.client_exceptions import ClientConnectionError, ServerDisconnectedError
from haffmpeg.camera import CameraMjpeg
from haffmpeg.tools import IMAGE_JPEG, ImageFrame
import onvif
from onvif import ONVIFCamera, exceptions
import voluptuous as vol
from zeep.exceptions import Fault
import homeassistant.components.persistent_notification as pn

from homeassistant.components.camera import PLATFORM_SCHEMA, SUPPORT_STREAM, Camera
from homeassistant.components.camera.const import DOMAIN
from homeassistant.components.ffmpeg import CONF_EXTRA_ARGUMENTS, DATA_FFMPEG
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
)
from homeassistant.exceptions import PlatformNotReady
from homeassistant.helpers.aiohttp_client import async_aiohttp_proxy_stream
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.service import async_extract_entity_ids
import homeassistant.util.dt as dt_util

from .const import (
    ABSOLUTE_MOVE,
    ATTR_CONTINUOUS_DURATION,
    ATTR_DISTANCE,
    ATTR_MOVE_MODE,
    ATTR_PAN,
    ATTR_PRESET_NAME,
    ATTR_PRESET_OPERATION,
    ATTR_PRESET_TOKEN,
    ATTR_PTZ_VECTOR,
    ATTR_SPEED,
    ATTR_SPEED_VECTOR,
    ATTR_TILT,
    ATTR_ZOOM,
    CONF_PROFILE_IDX,
    CONF_RTSP_TRANSPORT,
    CONF_CONTINUOUS_TIMEOUT_COMPLIANCE,
    CONTINUOUS_MOVE,
    DEFAULT_ARGUMENTS,
    DEFAULT_NAME,
    DEFAULT_PASSWORD,
    DEFAULT_PORT,
    DEFAULT_PROFILE_IDX,
    DEFAULT_USERNAME,
    DIR_DOWN,
    DIR_LEFT,
    DIR_RIGHT,
    DIR_UP,
    ENTITIES,
    GET_PRESETS,
    GOTO_HOME,
    GOTO_PRESET,
    ONVIF_DATA,
    PTZ_NONE,
    RELATIVE_MOVE,
    RTSP_TRANSPORT_HTTP,
    RTSP_TRANSPORT_RTSP,
    RTSP_TRANSPORT_UDP,
    SERVICE_ONVIF_CMD_REBOOT,
    SERVICE_PTZ_MOVE,
    SERVICE_PTZ_ADVANCED_MOVE,
    SERVICE_PTZ_PRESET,
    SET_HOME,
    SET_PRESET,
    STOP_MOVE,
    ZOOM_IN,
    ZOOM_OUT,
)

_LOGGER = logging.getLogger(__name__)

DOMAIN = "onvif"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_PASSWORD, default=DEFAULT_PASSWORD): cv.string,
        vol.Optional(CONF_USERNAME, default=DEFAULT_USERNAME): cv.string,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
        CONF_RTSP_TRANSPORT: vol.In(
            [RTSP_TRANSPORT_UDP, RTSP_TRANSPORT_HTTP, RTSP_TRANSPORT_RTSP]
        ),
        vol.Optional(CONF_EXTRA_ARGUMENTS, default=DEFAULT_ARGUMENTS): cv.string,
        vol.Optional(CONF_PROFILE_IDX, default=DEFAULT_PROFILE_IDX): vol.All(
            vol.Coerce(int), vol.Range(min=0)
        ),
        vol.Optional(CONF_CONTINUOUS_TIMEOUT_COMPLIANCE, default=True): cv.boolean,
    }
)

SERVICE_PTZ_MOVE_SCHEMA = vol.Schema(
    {
        ATTR_ENTITY_ID: cv.entity_ids,
        vol.Optional(ATTR_PAN, default=PTZ_NONE): vol.In([DIR_LEFT, DIR_RIGHT, PTZ_NONE]),
        vol.Optional(ATTR_TILT, default=PTZ_NONE): vol.In([DIR_UP, DIR_DOWN, PTZ_NONE]),
        vol.Optional(ATTR_ZOOM, default=PTZ_NONE): vol.In([ZOOM_OUT, ZOOM_IN, PTZ_NONE]),
        ATTR_MOVE_MODE: vol.In(
            [CONTINUOUS_MOVE, RELATIVE_MOVE, ABSOLUTE_MOVE,STOP_MOVE]
        ),
        vol.Optional(ATTR_CONTINUOUS_DURATION, default=0): cv.small_float,
        vol.Optional(ATTR_DISTANCE, default=0.1): cv.small_float,
        vol.Optional(ATTR_SPEED, default=0.5): cv.small_float,
    }
)

SERVICE_PTZ_ADVANCED_MOVE_SCHEMA = vol.Schema(
    {
        ATTR_ENTITY_ID: cv.entity_ids,

        ATTR_MOVE_MODE: vol.In(
            [CONTINUOUS_MOVE, RELATIVE_MOVE, ABSOLUTE_MOVE, STOP_MOVE]
        ),
        vol.Required(ATTR_PTZ_VECTOR): vol.All(
            vol.ExactSequence((cv.small_float, cv.small_float, cv.small_float)), vol.Coerce(tuple)
        ),
        vol.Optional(ATTR_SPEED_VECTOR, default=(1.0, 1.0, 1.0)): vol.All(
            vol.ExactSequence((cv.small_float, cv.small_float, cv.small_float)), vol.Coerce(tuple)
        ),
        vol.Optional(ATTR_CONTINUOUS_DURATION, default=0): cv.small_float
    }
)

SERVICE_PTZ_PRESET_SCHEMA = vol.Schema(
    {
        ATTR_ENTITY_ID: cv.entity_ids,
        ATTR_PRESET_OPERATION: vol.In(
            [GET_PRESETS, SET_PRESET, GOTO_PRESET, GOTO_HOME, SET_HOME]
        ),
        vol.Optional(ATTR_PRESET_NAME, default=""): cv.string,
        vol.Optional(ATTR_PRESET_TOKEN, default=""): cv.string,
    }
)

SERVICE_ONVIF_REBOOT_SCHEMA = vol.Schema({ATTR_ENTITY_ID: cv.entity_ids})


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up a ONVIF camera."""
    _LOGGER.debug("Setting up the ONVIF camera platform")

    async def async_handle_ptz_move(service):
        """Handle PTZ Move service call."""
        pan = service.data[ATTR_PAN]
        tilt = service.data[ATTR_TILT]
        zoom = service.data[ATTR_ZOOM]
        distance = service.data[ATTR_DISTANCE]
        speed = service.data[ATTR_SPEED]
        move_mode = service.data[ATTR_MOVE_MODE]
        continuous_timeout = service.data[ATTR_CONTINUOUS_DURATION]
        timeout_compliance = config[CONF_CONTINUOUS_TIMEOUT_COMPLIANCE]
        all_cameras = hass.data[ONVIF_DATA][ENTITIES]
        entity_ids = service.data[ATTR_ENTITY_ID]
        target_cameras = []
        target_cameras = [
            camera for camera in all_cameras if camera.entity_id in entity_ids
        ]
        for camera in target_cameras:
            await camera.async_perform_ptz_move(
                pan, tilt, zoom, distance, speed, move_mode, continuous_timeout, timeout_compliance
            )

    async def async_handle_ptz_advanced_move(service):
        """Handle PTZ Move service call."""
        ptz_vector = service.data[ATTR_PTZ_VECTOR]
        speed_vector = service.data[ATTR_SPEED_VECTOR]
        move_mode = service.data[ATTR_MOVE_MODE]
        continuous_timeout = service.data[ATTR_CONTINUOUS_DURATION]
        timeout_compliance = config[CONF_CONTINUOUS_TIMEOUT_COMPLIANCE]
        all_cameras = hass.data[ONVIF_DATA][ENTITIES]
        entity_ids = service.data[ATTR_ENTITY_ID]
        target_cameras = []
        target_cameras = [
            camera for camera in all_cameras if camera.entity_id in entity_ids
        ]
        for camera in target_cameras:
            await camera.async_perform_ptz_advanced_move(
                ptz_vector, speed_vector, move_mode, continuous_timeout,timeout_compliance
            )

    async def async_handle_ptz_preset(service):
        """Handle PTZ Preset service call."""
        preset_operation = service.data[ATTR_PRESET_OPERATION]
        preset_name = service.data[ATTR_PRESET_NAME]
        preset_token = service.data[ATTR_PRESET_TOKEN]
        all_cameras = hass.data[ONVIF_DATA][ENTITIES]
        entity_ids = await async_extract_entity_ids(hass, service)
        target_cameras = []
        target_cameras = [
            camera for camera in all_cameras if camera.entity_id in entity_ids
        ]
        for camera in target_cameras:
            await camera.async_perform_ptz_preset(
                preset_operation, preset_name, preset_token
            )

    async def async_handle_reboot(service):
        """Handle ONVIF Reboot service call."""
        all_cameras = hass.data[ONVIF_DATA][ENTITIES]
        entity_ids = await async_extract_entity_ids(hass, service)
        target_cameras = []
        target_cameras = [
            camera for camera in all_cameras if camera.entity_id in entity_ids
        ]
        for camera in target_cameras:
            await camera.async_perform_reboot()

    hass.services.async_register(
        DOMAIN, SERVICE_PTZ_MOVE, async_handle_ptz_move, schema=SERVICE_PTZ_MOVE_SCHEMA
    )

    hass.services.async_register(
        DOMAIN, SERVICE_PTZ_ADVANCED_MOVE, async_handle_ptz_advanced_move, schema=SERVICE_PTZ_ADVANCED_MOVE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_PTZ_PRESET,
        async_handle_ptz_preset,
        schema=SERVICE_PTZ_PRESET_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ONVIF_CMD_REBOOT,
        async_handle_reboot,
        schema=SERVICE_ONVIF_REBOOT_SCHEMA,
    )

    _LOGGER.debug("Constructing the ONVIFHassCamera")

    hass_camera = ONVIFHassCamera(hass, config)

    await hass_camera.async_initialize()

    async_add_entities([hass_camera])
    return


class ONVIFHassCamera(Camera):
    """An implementation of an ONVIF camera."""

    def __init__(self, hass, config):
        """Initialize an ONVIF camera."""
        super().__init__()

        _LOGGER.debug("Importing dependencies")

        _LOGGER.debug("Setting up the ONVIF camera component")

        self._username = config.get(CONF_USERNAME)
        self._password = config.get(CONF_PASSWORD)
        self._host = config.get(CONF_HOST)
        self._port = config.get(CONF_PORT)
        self._name = config.get(CONF_NAME)
        self._ffmpeg_arguments = config.get(CONF_EXTRA_ARGUMENTS)
        self._profile_index = config.get(CONF_PROFILE_IDX)
        self._rtsp_transport = config.get(CONF_RTSP_TRANSPORT)
        self._media_service = None
        self._ptz_service = None
        self._image_service = None
        self._input_uri = None
        self._input_uri_for_log = None
        self._profile_token = None
        self._profiles = None
        self._ptz_opt = None
        self._ptz_presets = None

        _LOGGER.debug(
            "Setting up the ONVIF camera device @ '%s:%s'", self._host, self._port
        )

        self._camera = ONVIFCamera(
            self._host,
            self._port,
            self._username,
            self._password,
            "{}/wsdl/".format(os.path.dirname(onvif.__file__)),
        )

    async def async_initialize(self):
        """
        Initialize the camera.

        Initializes the camera by obtaining the input uri and connecting to
        the camera. Also retrieves the ONVIF profiles.
        """
        try:
            _LOGGER.debug("Updating service addresses")
            await self._camera.update_xaddrs()
            await self.async_check_date_and_time()

            self._media_service = await self.async_obtain_media_service()
            self._profiles = await self.async_obtain_profiles()
            self._profile_token = self.index_to_profile_token()
            await self.async_obtain_input_uri()
            self._ptz_service = await self.async_obtain_ptz_service()

        except ClientConnectionError as err:
            _LOGGER.warning(
                "Couldn't connect to camera '%s', but will retry later. Error: %s",
                self._name,
                err,
            )
            raise PlatformNotReady
        except Fault as err:
            _LOGGER.error(
                "Couldn't connect to camera '%s', please verify "
                "that the credentials are correct. Error: %s",
                self._name,
                err,
            )

    async def async_check_date_and_time(self):
        """Warns if camera and system date not synced."""
        _LOGGER.debug("Setting up the ONVIF device management service")
        devicemgmt = self._camera.create_devicemgmt_service()

        _LOGGER.debug("Retrieving current camera date/time")
        try:
            system_date = dt_util.utcnow()
            device_time = await devicemgmt.GetSystemDateAndTime()
            if not device_time:
                _LOGGER.debug(
                    """Couldn't get camera '%s' date/time.
                    GetSystemDateAndTime() return null/empty""",
                    self._name,
                )
                return

            if device_time.UTCDateTime:
                tzone = dt_util.UTC
                cdate = device_time.UTCDateTime
            else:
                tzone = (
                    dt_util.get_time_zone(device_time.TimeZone)
                    or dt_util.DEFAULT_TIME_ZONE
                )
                cdate = device_time.LocalDateTime

            if cdate is None:
                _LOGGER.warning("Could not retrieve date/time on this camera")
            else:
                cam_date = dt.datetime(
                    cdate.Date.Year,
                    cdate.Date.Month,
                    cdate.Date.Day,
                    cdate.Time.Hour,
                    cdate.Time.Minute,
                    cdate.Time.Second,
                    0,
                    tzone,
                )

                cam_date_utc = cam_date.astimezone(dt_util.UTC)

                _LOGGER.debug("TimeZone for date/time: %s", tzone)

                _LOGGER.debug("Camera date/time: %s", cam_date)

                _LOGGER.debug("Camera date/time in UTC: %s", cam_date_utc)

                _LOGGER.debug("System date/time: %s", system_date)

                dt_diff = cam_date - system_date
                dt_diff_seconds = dt_diff.total_seconds()

                if dt_diff_seconds > 5:
                    _LOGGER.warning(
                        "The date/time on the camera (UTC) is '%s', "
                        "which is different from the system '%s', "
                        "this could lead to authentication issues",
                        cam_date_utc,
                        system_date,
                    )
        except ServerDisconnectedError as err:
            _LOGGER.warning(
                "Couldn't get camera '%s' date/time. Error: %s", self._name, err
            )

    async def async_obtain_profiles(self):
        """Obtain onvif profiles object."""
        try:
            __profiles = await self._media_service.GetProfiles()
            _LOGGER.debug("Retrieved '%d' profiles", len(__profiles))
            return __profiles
        except exceptions.ONVIFError as err:
            _LOGGER.error(
                "Couldn't retrieve profiles of camera '%s'. Error: %s", self._name, err,
            )
            return None

    async def async_obtain_media_service(self):
        """Obtain onvif profiles object."""
        try:
            _LOGGER.debug(
                "Connecting with ONVIF Camera: %s on port %s", self._host, self._port
            )
            __media_service = self._camera.create_media_service()
            return __media_service
        except exceptions.ONVIFError as err:
            _LOGGER.error(
                "Couldn't retrieve media_service of camera '%s'. Error: %s",
                self._name,
                err,
            )
            return None

    def index_to_profile_token(self):
        """Return token name from a index over profiles object."""
        if self._profile_index >= len(self._profiles):
            _LOGGER.warning(
                "ONVIF Camera '%s' doesn't provide profile %d."
                " Using the last profile.",
                self._name,
                self._profile_index,
            )
            self._profile_index = -1

        _LOGGER.debug("Using profile index '%d'", self._profile_index)
        return self._profiles[self._profile_index].token

    async def async_obtain_input_uri(self):
        """Set the input uri for the camera."""
        _LOGGER.debug("Retrieving stream uri")
        # Fix Onvif setup error on Goke GK7102 based IP camera #26781
        # Assume not buggy camera and see if ClientConnectionError
        # reload fresh self._media_service before one retry
        for i in range(0, 2):
            try:
                req = self._media_service.create_type("GetStreamUri")
                req.ProfileToken = self._profile_token
                req.StreamSetup = {
                    "Stream": "RTP-Unicast",
                    "Transport": {"Protocol": self._rtsp_transport},
                }

                stream_uri = await self._media_service.GetStreamUri(req)
                uri_no_auth = stream_uri.Uri
                self._input_uri_for_log = uri_no_auth.replace(
                    "rtsp://", "rtsp://<user>:<password>@", 1
                )

                self._input_uri = uri_no_auth.replace(
                    "rtsp://", "rtsp://%s:%s@" % (self._username, self._password), 1
                )

                _LOGGER.debug(
                    "ONVIF Camera Using the following URL for %s: %s",
                    self._name,
                    self._input_uri_for_log,
                )
                break
            except ClientConnectionError as err:
                if i == 0:
                    _LOGGER.info(
                        "GetStreamUri on '%s'. Error: %s. Trying a workaround against known issue(#26781)",
                        self._name,
                        err,
                    )
                    self._media_service = self._camera.create_media_service()
                    pass
                else:
                    _LOGGER.error(
                        "Couldn't setup camera '%s'. Error: %s", self._name, err
                    )
                    return (None, None)

    async def async_obtain_ptz_service(self):
        """Set up PTZ service if available."""
        _LOGGER.debug("Setting up the ONVIF PTZ service")
        if self._camera.get_service("ptz") is None:
            _LOGGER.debug("PTZ is not available")
            return None
        else:
            _LOGGER.debug("Completed set up of the ONVIF camera component")
            return self._camera.create_ptz_service()

    async def async_perform_ptz_move(
        self, pan, tilt, zoom, distance, speed, move_mode, continuous_timeout, timeout_compliance
    ):
        """Perform legacy PTZ actions on the camera + new move_modes"""
        pan_val = (
            distance if pan == DIR_RIGHT else -distance if pan == DIR_LEFT else 0
        )
        tilt_val = (
            distance if tilt == DIR_UP else -distance if tilt == DIR_DOWN else 0
        )
        zoom_val = (
            distance if zoom == ZOOM_IN else -distance if zoom == ZOOM_OUT else 0
        )
        speed_val = speed
        await self.async_perform_ptz_advanced_move( (pan_val, tilt_val, zoom_val), (speed_val,speed_val,speed_val),move_mode,continuous_timeout,timeout_compliance )


    async def async_perform_ptz_advanced_move(
        self, ptz_vector, speed_vector, move_mode, continuous_timeout, timeout_compliance
    ):
        """Perform a PTZ action on the camera."""
        if self._ptz_service is None:
            _LOGGER.warning(
                "PTZ Move actions are not supported on camera '%s'", self._name
            )
            return

        if self._ptz_service:
            pan_val = ptz_vector[0]
            tilt_val = ptz_vector[1]
            zoom_val = ptz_vector[2]
            _LOGGER.debug(
                "Calling %s PTZ Move on camera '%s'| Pan = %4.2f | Tilt = %4.2f | Zoom = %4.2f | Speed = %s | Timeout = %1.1f",
                move_mode,
                self._name,
                ptz_vector[0],
                ptz_vector[1],
                ptz_vector[2],
                speed_vector,
                continuous_timeout
            )
            try:
                req = self._ptz_service.create_type(move_mode)
                req.ProfileToken = self._profile_token

                if move_mode == CONTINUOUS_MOVE:
                    req.Velocity = {
                        "PanTilt": {"x": pan_val, "y": tilt_val},
                        "Zoom": {"x": zoom_val},
                    }
                    if continuous_timeout != 0:
                        req.Timeout = dt.timedelta(0, 0, continuous_timeout * 1000000)
                    await self._ptz_service.ContinuousMove(req)
                    if continuous_timeout != 0 and not timeout_compliance:
                        await asyncio.sleep(continuous_timeout)
                        req = self._ptz_service.create_type("Stop")
                        req.ProfileToken = self._profile_token
                        await self._ptz_service.Stop(req)

                elif move_mode == RELATIVE_MOVE:
                    req.Translation = {
                        "PanTilt": {"x": pan_val, "y": tilt_val},
                        "Zoom": {"x": zoom_val},
                    }
                    req.Speed = {
                        "PanTilt": {"x": speed_vector[0], "y": speed_vector[1]},
                        "Zoom": {"x": speed_vector[2]},
                    }
                    await self._ptz_service.RelativeMove(req)

                elif move_mode == ABSOLUTE_MOVE:
                    req.Position = {
                        "PanTilt": {"x": pan_val, "y": tilt_val},
                        "Zoom": {"x": zoom_val},
                    }
                    req.Speed = {
                        "PanTilt": {"x": speed_vector[0], "y": speed_vector[1]},
                        "Zoom": {"x": speed_vector[2]},
                    }
                    await self._ptz_service.AbsoluteMove(req)

            except exceptions.ONVIFError as err:
                if "Bad Request" in err.reason:
                    self._ptz_service = None
                    _LOGGER.debug("Camera '%s' doesn't support PTZ.", self._name)
        else:
            _LOGGER.debug("Camera '%s' doesn't support PTZ.", self._name)


    async def async_perform_ptz_preset(
        self, preset_operation, preset_name, preset_token
    ):
        """Perform a PTZ Preset action on the camera."""
        if self._ptz_service:
            if preset_operation in (
                GOTO_HOME,
                SET_HOME,
                GOTO_PRESET,
                SET_PRESET,
                GET_PRESETS,
            ):
                try:
                    _LOGGER.debug("Retrieved PTZ presets")
                    req = self._ptz_service.create_type(GET_PRESETS)
                    req.ProfileToken = self._profile_token
                    __presets = await self._ptz_service.GetPresets(req)

                    _LOGGER.debug(
                        "Calling PTZ preset| Operation = %s | PresetName = %s | PresetToken = %s",
                        preset_operation,
                        preset_name,
                        preset_token,
                    )

                    req = self._ptz_service.create_type(preset_operation)
                    req.ProfileToken = self._profile_token

                    if preset_operation == GOTO_PRESET:
                        preset_token = next(
                            (
                                preset["token"]
                                for preset in __presets
                                if preset["Name"] == preset_name
                            ),
                            None,
                        )
                        _LOGGER.debug(
                            "PresetToken from PresetName | PresetName = %s | PresetToken = %s",
                            preset_name,
                            preset_token,
                        )
                        req.PresetToken = "%s" % preset_token
                        req.Speed = {
                            "PanTilt": {"x": 1.0, "y": 1.0},
                            "Zoom": {"x": 1.0},
                        }
                        await self._ptz_service.GotoPreset(req)

                    if preset_operation == SET_PRESET:
                        req.PresetToken = preset_token
                        req.PresetName = preset_name
                        await self._ptz_service.SetPreset(req)

                    if preset_operation == GET_PRESETS:
                        presets = []
                        if __presets is not None:
                            for preset in __presets:
                                presets.append(preset["Name"])
                        pn.create(self.hass, "\n".join(presets), title="Onvif PTZ Presets")

                    if preset_operation == GOTO_HOME:
                        await self._ptz_service.GotoHomePosition(req)

                    if preset_operation == SET_HOME:
                        await self._ptz_service.SetHomePosition(req)

                except exceptions.ONVIFError as err:
                    if "Bad Request" in err.reason:
                        _LOGGER.error(
                            "Camera '%s' doesn't support PTZ %s operation.",
                            self._name,
                            preset_operation,
                        )
                except Exception as err:
                    _LOGGER.info(
                        "Camera '%s' PTZ %s operation failed with that reason: %s",
                        self._name,
                        preset_operation,
                        err,
                    )
            else:
                _LOGGER.debug("PTZ %s operation is not implemented", preset_operation)
        else:
            self._ptz_service = None
            _LOGGER.debug("Camera '%s' doesn't support PTZ.", self._name)

    async def async_perform_reboot(self):
        """Perform a SystemReboot action on the camera."""
        try:
            _LOGGER.debug("Calling SystemReboot")
            ret = await self._camera.devicemgmt.SystemReboot()
            _LOGGER.debug("Camera '%s' Reboot command returned '%s'", self._name, ret)
        except exceptions.ONVIFError as err:
            _LOGGER.error(
                "Couldn't reboot the camera '%s', please verify "
                "that the camera supports the command. Error: %s",
                self._name,
                err,
            )

    async def async_added_to_hass(self):
        """Handle entity addition to hass."""
        _LOGGER.debug("Camera '%s' added to hass", self._name)
        if ONVIF_DATA not in self.hass.data:
            self.hass.data[ONVIF_DATA] = {}
            self.hass.data[ONVIF_DATA][ENTITIES] = []
        self.hass.data[ONVIF_DATA][ENTITIES].append(self)

    async def async_camera_image(self):
        """Return a still image response from the camera."""

        _LOGGER.debug("Retrieving image from camera '%s'", self._name)

        ffmpeg = ImageFrame(self.hass.data[DATA_FFMPEG].binary, loop=self.hass.loop)

        image = await asyncio.shield(
            ffmpeg.get_image(
                self._input_uri,
                output_format=IMAGE_JPEG,
                extra_cmd=self._ffmpeg_arguments,
            )
        )
        return image

    async def handle_async_mjpeg_stream(self, request):
        """Generate an HTTP MJPEG stream from the camera."""
        _LOGGER.debug("Handling mjpeg stream from camera '%s'", self._name)

        ffmpeg_manager = self.hass.data[DATA_FFMPEG]
        stream = CameraMjpeg(ffmpeg_manager.binary, loop=self.hass.loop)

        await stream.open_camera(self._input_uri, extra_cmd=self._ffmpeg_arguments)

        try:
            stream_reader = await stream.get_reader()
            return await async_aiohttp_proxy_stream(
                self.hass,
                request,
                stream_reader,
                ffmpeg_manager.ffmpeg_stream_content_type,
            )
        finally:
            await stream.close()

    @property
    def supported_features(self):
        """Return supported features."""
        if self._input_uri:
            return SUPPORT_STREAM
        return 0

    async def stream_source(self):
        """Return the stream source."""
        return self._input_uri

    @property
    def name(self):
        """Return the name of this camera."""
        return self._name
