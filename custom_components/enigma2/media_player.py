"""Support for Enigma2 media players."""

import asyncio
import contextlib
from http import HTTPStatus
from logging import getLogger
from typing import override

from aiohttp import ClientError, ClientTimeout
from aiohttp.client_exceptions import ServerDisconnectedError
from openwebif.enums import PowerState, RemoteControlCodes, SetVolumeOption

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import Enigma2ConfigEntry, Enigma2UpdateCoordinator

ATTR_MEDIA_CURRENTLY_RECORDING = "media_currently_recording"
ATTR_MEDIA_DESCRIPTION = "media_description"
ATTR_MEDIA_END_TIME = "media_end_time"
ATTR_MEDIA_START_TIME = "media_start_time"

_LOGGER = getLogger(__name__)

SCREENSHOT_PATH = "/grab?format=jpg&r=480"
SCREENSHOT_TIMEOUT = ClientTimeout(total=10)
SCREENSHOT_REFRESH_INTERVAL = 30


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Enigma2ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Enigma2 media player platform."""
    async_add_entities([Enigma2Device(entry.runtime_data)])


class Enigma2Device(CoordinatorEntity[Enigma2UpdateCoordinator], MediaPlayerEntity):
    """Representation of an Enigma2 box."""

    _attr_has_entity_name = True
    _attr_name = None

    _attr_media_content_type = MediaType.TVSHOW
    _attr_supported_features = (
        MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.TURN_OFF
        | MediaPlayerEntityFeature.NEXT_TRACK
        | MediaPlayerEntityFeature.STOP
        | MediaPlayerEntityFeature.PREVIOUS_TRACK
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.SELECT_SOURCE
        | MediaPlayerEntityFeature.PLAY
    )

    def __init__(self, coordinator: Enigma2UpdateCoordinator) -> None:
        """Initialize the Enigma2 device."""

        super().__init__(coordinator)

        self._attr_unique_id = coordinator.unique_id

        self._attr_device_info = coordinator.device_info
        self._screenshot_revision = 0
        self._last_service_ref: str | None = None
        self._cancel_screenshot_refresh = None
        self._screenshot_cache_revision = -1
        self._screenshot_cache: bytes | None = None
        self._screenshot_content_type: str | None = None
        self._screenshot_lock = asyncio.Lock()

    @override
    async def async_added_to_hass(self) -> None:
        """Initialize the image and register the screenshot refresh timer."""
        await super().async_added_to_hass()
        if not self.coordinator.data.in_standby:
            self._last_service_ref = self.coordinator.data.currservice.serviceref
            self._invalidate_screenshot()
            self._schedule_screenshot_refresh()

    @override
    async def async_will_remove_from_hass(self) -> None:
        """Cancel the screenshot timer when the entity is removed."""
        self._cancel_screenshot_timer()
        await super().async_will_remove_from_hass()

    @callback
    def _cancel_screenshot_timer(self) -> None:
        """Cancel a pending screenshot refresh timer."""
        if self._cancel_screenshot_refresh is not None:
            self._cancel_screenshot_refresh()
            self._cancel_screenshot_refresh = None

    @callback
    def _schedule_screenshot_refresh(self) -> None:
        """Schedule the next screenshot revision exactly 30 seconds later."""
        self._cancel_screenshot_timer()
        if self.coordinator.data.in_standby:
            return
        self._cancel_screenshot_refresh = async_call_later(
            self.hass, SCREENSHOT_REFRESH_INTERVAL, self._handle_screenshot_timer
        )

    @callback
    def _handle_screenshot_timer(self, _now) -> None:
        """Invalidate the screenshot every 30 seconds while the box is on."""
        self._cancel_screenshot_refresh = None
        if self.coordinator.data.in_standby:
            return
        self._invalidate_screenshot()
        self.async_write_ha_state()
        self._schedule_screenshot_refresh()

    @callback
    def _invalidate_screenshot(self) -> None:
        """Create a new image revision and invalidate its in-memory cache."""
        self._screenshot_revision += 1
        self._attr_media_image_hash = str(self._screenshot_revision)
        self._screenshot_cache_revision = -1
        self._screenshot_cache = None
        self._screenshot_content_type = None

    @override
    async def async_get_media_image(self) -> tuple[bytes | None, str | None]:
        """Return the screenshot for the current image revision.

        A revision is created only after a channel change or every 30 seconds.
        The resulting image is cached in memory so multiple frontend requests for
        the same revision do not cause additional /grab requests to OpenWebif.
        """
        if self.coordinator.data.in_standby:
            return None, None

        revision = self._screenshot_revision
        if self._screenshot_cache_revision == revision:
            return self._screenshot_cache, self._screenshot_content_type

        async with self._screenshot_lock:
            if self._screenshot_cache_revision == revision:
                return self._screenshot_cache, self._screenshot_content_type

            content: bytes | None = None
            content_type: str | None = None
            try:
                async with self.coordinator.session.get(
                    SCREENSHOT_PATH, timeout=SCREENSHOT_TIMEOUT
                ) as response:
                    if response.status == HTTPStatus.OK:
                        content = await response.read()
                        content_type = response.headers.get(
                            "Content-Type", "image/jpeg"
                        ).split(";", 1)[0]
                    else:
                        _LOGGER.debug(
                            "OpenWebif screenshot request failed with HTTP status %s",
                            response.status,
                        )
            except (ClientError, TimeoutError) as err:
                _LOGGER.debug("Unable to retrieve OpenWebif screenshot: %s", err)

            # Cache success as well as failure for this revision. This guarantees
            # that OpenWebif is contacted at most once per screenshot revision.
            if revision == self._screenshot_revision:
                self._screenshot_cache_revision = revision
                self._screenshot_cache = content
                self._screenshot_content_type = content_type

            return content, content_type

    @override
    async def async_turn_off(self) -> None:
        """Turn off media player."""
        if self.coordinator.device.turn_off_to_deep:
            # pylint: disable-next=home-assistant-action-swallowed-exception
            with contextlib.suppress(ServerDisconnectedError):
                await self.coordinator.device.set_powerstate(PowerState.DEEP_STANDBY)
            self._attr_available = False
        else:
            await self.coordinator.device.set_powerstate(PowerState.STANDBY)
            await self.coordinator.async_refresh()

    @override
    async def async_turn_on(self) -> None:
        """Turn the media player on."""
        await self.coordinator.device.turn_on()
        await self.coordinator.async_refresh()

    @override
    async def async_set_volume_level(self, volume: float) -> None:
        """Set volume level, range 0..1."""
        await self.coordinator.device.set_volume(int(volume * 100))
        await self.coordinator.async_refresh()

    @override
    async def async_volume_up(self) -> None:
        """Volume up the media player."""
        await self.coordinator.device.set_volume(SetVolumeOption.UP)
        await self.coordinator.async_refresh()

    @override
    async def async_volume_down(self) -> None:
        """Volume down media player."""
        await self.coordinator.device.set_volume(SetVolumeOption.DOWN)
        await self.coordinator.async_refresh()

    @override
    async def async_media_stop(self) -> None:
        """Send stop command."""
        await self.coordinator.device.send_remote_control_action(
            RemoteControlCodes.STOP
        )
        await self.coordinator.async_refresh()

    @override
    async def async_media_play(self) -> None:
        """Play media."""
        await self.coordinator.device.send_remote_control_action(
            RemoteControlCodes.PLAY
        )
        await self.coordinator.async_refresh()

    @override
    async def async_media_pause(self) -> None:
        """Pause the media player."""
        await self.coordinator.device.send_remote_control_action(
            RemoteControlCodes.PAUSE
        )
        await self.coordinator.async_refresh()

    @override
    async def async_media_next_track(self) -> None:
        """Send next track command."""
        await self.coordinator.device.send_remote_control_action(
            RemoteControlCodes.CHANNEL_UP
        )
        await self.coordinator.async_refresh()

    @override
    async def async_media_previous_track(self) -> None:
        """Send previous track command."""
        await self.coordinator.device.send_remote_control_action(
            RemoteControlCodes.CHANNEL_DOWN
        )
        await self.coordinator.async_refresh()

    @override
    async def async_mute_volume(self, mute: bool) -> None:
        """Mute or unmute."""
        if mute != self.coordinator.data.muted:
            await self.coordinator.device.toggle_mute()
            await self.coordinator.async_refresh()

    @override
    async def async_select_source(self, source: str) -> None:
        """Select input source."""
        await self.coordinator.device.zap(self.coordinator.device.sources[source])
        await self.coordinator.async_refresh()

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        """Update state of the media_player."""

        if not self.coordinator.data.in_standby:
            self._attr_extra_state_attributes = {
                ATTR_MEDIA_CURRENTLY_RECORDING: self.coordinator.data.is_recording,
                ATTR_MEDIA_DESCRIPTION: (
                    self.coordinator.data.currservice.fulldescription
                ),
                ATTR_MEDIA_START_TIME: self.coordinator.data.currservice.begin,
                ATTR_MEDIA_END_TIME: self.coordinator.data.currservice.end,
            }
        else:
            self._attr_extra_state_attributes = {}

        self._attr_media_title = self.coordinator.data.currservice.station
        self._attr_media_series_title = self.coordinator.data.currservice.name
        self._attr_media_channel = self.coordinator.data.currservice.station
        self._attr_is_volume_muted = self.coordinator.data.muted
        self._attr_media_content_id = self.coordinator.data.currservice.serviceref

        service_ref = self.coordinator.data.currservice.serviceref

        # Request a new screenshot immediately when the channel changes.
        # Otherwise the timer below invalidates it exactly every 30 seconds.
        if self.coordinator.data.in_standby:
            self._attr_media_image_hash = None
            self._last_service_ref = None
            self._cancel_screenshot_timer()
        elif service_ref != self._last_service_ref:
            self._last_service_ref = service_ref
            self._invalidate_screenshot()
            self._schedule_screenshot_refresh()

        self._attr_source = self.coordinator.data.currservice.station
        self._attr_source_list = self.coordinator.device.source_list

        if self.coordinator.data.in_standby:
            self._attr_state = MediaPlayerState.OFF
        else:
            self._attr_state = MediaPlayerState.ON

        if (volume_level := self.coordinator.data.volume) is not None:
            self._attr_volume_level = volume_level / 100
        else:
            self._attr_volume_level = None

        self.async_write_ha_state()
