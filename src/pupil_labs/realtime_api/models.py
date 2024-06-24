import enum
import logging
import typing as T
from dataclasses import field
from datetime import datetime
from uuid import UUID

from pydantic import confloat, conint
from pydantic.dataclasses import dataclass

try:
    from typing import Literal
except ImportError:
    # FIXME: Remove when dropping py3.7 support
    from typing_extensions import Literal

logger = logging.getLogger(__name__)


class APIPath(enum.Enum):
    STATUS = "/status"
    RECORDING_START = "/recording:start"
    RECORDING_STOP_AND_SAVE = "/recording:stop_and_save"
    RECORDING_CANCEL = "/recording:cancel"
    EVENT = "/event"
    CALIBRATION = "/../calibration.bin"
    TEMPLATE_DEFINITION = "/template_def"
    TEMPLATE_DATA = "/template_data"

    def full_address(
        self, address: str, port: int, protocol: str = "http", prefix: str = "/api"
    ) -> str:
        return f"{protocol}://{address}:{port}" + prefix + self.value


class DiscoveredDeviceInfo(T.NamedTuple):
    name: str
    """Full mDNS service name.
    Follows ``'PI monitor:<phone name>:<hardware id>._http._tcp.local.'`` naming pattern
    """
    server: str
    "e.g. ``'pi.local.'``"
    port: int
    "e.g. `8080`"
    addresses: T.List[str]
    "e.g. ``['192.168.0.2']``"


class Event(T.NamedTuple):
    name: T.Optional[str]
    recording_id: T.Optional[str]
    timestamp: int  # unix epoch, in nanoseconds

    @classmethod
    def from_dict(cls, dct: T.Dict[str, T.Any]) -> "Event":
        return cls(
            name=dct.get("name"),
            recording_id=dct.get("recording_id"),
            timestamp=dct["timestamp"],
        )

    @property
    def datetime(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp / 1e9)

    def __repr__(self) -> str:
        return (
            f"Event(name={self.name} "
            f"recording_id={self.recording_id} "
            f"timestamp_unix_ns={self.timestamp} "
            f"datetime={self.datetime})"
        )


class Phone(T.NamedTuple):
    battery_level: int
    battery_state: Literal["OK", "LOW", "CRITICAL"]
    device_id: str
    device_name: str
    ip: str
    memory: int
    memory_state: Literal["OK", "LOW", "CRITICAL"]
    time_echo_port: T.Optional[int] = None


class Hardware(T.NamedTuple):
    version: str = "unknown"
    glasses_serial: str = "unknown"
    world_camera_serial: str = "unknown"
    module_serial: str = "unknown"


class NetworkDevice(T.NamedTuple):
    """Information about devices discovered by the host device, not the client.

    .. note::
        This class represents device information made available via the websocket update
        connection by the host device (exposed via
        :py:meth:`pupil_labs.realtime_api.device.Device.status_updates`). Devices
        discovered directly by this library are represented as
        :py:class:`.DiscoveredDeviceInfo` and returned by
        :py:func:`pupil_labs.realtime_api.discovery.discover_devices` and
        :py:class:`pupil_labs.realtime_api.discovery.Network`.
    """

    ip: str
    device_id: str
    device_name: str
    connected: bool


class Sensor(T.NamedTuple):
    sensor: str
    conn_type: str
    connected: bool = False
    ip: T.Optional[str] = None
    params: T.Optional[str] = None
    port: T.Optional[int] = None
    protocol: str = "rtsp"

    @property
    def url(self) -> T.Optional[str]:
        if self.connected:
            return f"{self.protocol}://{self.ip}:{self.port}/?{self.params}"
        return None

    class Name(enum.Enum):
        ANY = None
        GAZE = "gaze"
        WORLD = "world"
        IMU = "imu"
        EYES = "eyes"

    class Connection(enum.Enum):
        ANY = None
        WEBSOCKET = "WEBSOCKET"
        DIRECT = "DIRECT"


class Recording(T.NamedTuple):
    action: str
    id: str
    message: str
    rec_duration_ns: int

    @property
    def rec_duration_seconds(self) -> float:
        return self.rec_duration_ns / 1e9


Component = T.Union[Phone, Hardware, Sensor, Recording, NetworkDevice]
"""Type annotation for :py:class:`Status <.Status>` components."""

ComponentRaw = T.Dict[str, T.Any]
"""Type annotation for json-parsed responses from the REST and Websocket API."""

_model_class_map: T.Dict[str, T.Type[Component]] = {
    "Phone": Phone,
    "Hardware": Hardware,
    "Sensor": Sensor,
    "Recording": Recording,
    "Event": Event,
    "NetworkDevice": NetworkDevice,
}


def _init_cls_with_annotated_fields_only(cls, d: T.Dict[str, T.Any]):
    return cls(**{attr: d.get(attr, None) for attr in cls.__annotations__})


class UnknownComponentError(ValueError):
    pass


def parse_component(raw: ComponentRaw) -> Component:
    """Initialize an explicitly modelled representation
    (:py:obj:`pupil_labs.realtime_api.models.Component`) from the json-parsed dictionary
    (:py:obj:`pupil_labs.realtime_api.models.ComponentRaw`) received from the API.

    :raises UnknownComponentError: if the component name cannot be mapped to an
        explicitly modelled class or the contained data does not fit the modelled
        fields.
    """
    model_name = raw["model"]
    data = raw["data"]
    try:
        model_class = _model_class_map[model_name]
        return _init_cls_with_annotated_fields_only(model_class, data)
    except KeyError as err:
        raise UnknownComponentError(
            f"Could not generate component for {model_name} from {data}"
        ) from err


@dataclass
class Status:
    "Represents the Companion's full status"

    phone: Phone
    hardware: Hardware
    sensors: T.List[Sensor]
    recording: T.Optional[Recording]

    @classmethod
    def from_dict(cls, status_json_result: T.List[ComponentRaw]) -> "Status":
        phone = None  # always present
        recording = None  # might not be present
        hardware = Hardware()  # won't be present if glasses are not connected
        sensors = []
        for dct in status_json_result:
            try:
                component = parse_component(dct)
            except UnknownComponentError:
                logger.warning(f"Dropping unknown component: {dct}")
                continue
            if isinstance(component, Phone):
                phone = component
            elif isinstance(component, Hardware):
                hardware = component
            elif isinstance(component, Sensor):
                sensors.append(component)
            elif isinstance(component, Recording):
                recording = component
            elif isinstance(component, NetworkDevice):
                pass  # no need to handle NetworkDevice updates here
            else:
                logger.warning(f"Unknown model class: {type(component).__name__}")
        sensors.sort(key=lambda s: (not s.connected, s.conn_type, s.sensor))
        return cls(phone, hardware, sensors, recording)

    def update(self, component: Component) -> None:
        if isinstance(component, Phone):
            self.phone = component
        elif isinstance(component, Hardware):
            self.hardware = component
        elif isinstance(component, Recording):
            self.recording = component
        elif isinstance(component, Sensor):
            for idx, sensor in enumerate(self.sensors):
                if (
                    sensor.sensor == component.sensor
                    and sensor.conn_type == component.conn_type
                ):
                    self.sensors[idx] = component
                    break

    def matching_sensors(self, name: Sensor.Name, connection: Sensor.Connection):
        for sensor in self.sensors:
            if name is not Sensor.Name.ANY and sensor.sensor != name.value:
                continue
            if (
                connection is not Sensor.Connection.ANY
                and sensor.conn_type != connection.value
            ):
                continue
            yield sensor

    def direct_world_sensor(self) -> T.Optional[Sensor]:
        return next(
            self.matching_sensors(Sensor.Name.WORLD, Sensor.Connection.DIRECT),
            Sensor(
                sensor=Sensor.Name.WORLD.value, conn_type=Sensor.Connection.DIRECT.value
            ),
        )

    def direct_gaze_sensor(self) -> T.Optional[Sensor]:
        return next(
            self.matching_sensors(Sensor.Name.GAZE, Sensor.Connection.DIRECT),
            Sensor(
                sensor=Sensor.Name.GAZE.value, conn_type=Sensor.Connection.DIRECT.value
            ),
        )

    def direct_imu_sensor(self) -> T.Optional[Sensor]:
        return next(
            self.matching_sensors(Sensor.Name.IMU, Sensor.Connection.DIRECT),
            Sensor(
                sensor=Sensor.Name.IMU.value, conn_type=Sensor.Connection.DIRECT.value
            ),
        )

    def direct_eyes_sensor(self) -> T.Optional[Sensor]:
        return next(
            self.matching_sensors(Sensor.Name.EYES, Sensor.Connection.DIRECT),
            Sensor(
                sensor=Sensor.Name.EYES.value, conn_type=Sensor.Connection.DIRECT.value
            ),
        )


TemplateItemWidgetType = Literal[
    "TEXT", "PARAGRAPH", "RADIO_LIST", "CHECKBOX_LIST", "SECTION_HEADER", "PAGE_BREAK"
]
TemplateItemInputType = T.Literal["any", "integer", "float"]


@dataclass
class TemplateItem:
    choices: T.Optional[T.List[str]]
    help_text: T.Optional[str]
    id: UUID
    required: bool
    title: str
    widget_type: TemplateItemWidgetType
    input_type: TemplateItemInputType

    def validate_value(self, key: UUID, value: T.Any):
        if self.input_type != "any":
            try:
                if self.input_type == "integer":
                    value = conint(strict=True)(value)
                elif self.input_type == "float":
                    value = confloat(strict=True)(value)
            except ValueError as e:
                raise ValueError(
                    f"{self.title}[{key}]: Invalid input type"
                    + f", it should be {self.input_type}, ValueError: {e}"
                )
        else:
            if self.widget_type in ["RADIO_LIST", "CHECKBOX_LIST"]:
                if str(value) not in self.choices:
                    raise ValueError(
                        f"{self.title}[{key}]: Invalid choice {value}."
                        + f" Must be one of {', '.join(self.choices)}."
                    )


@dataclass(kw_only=True)
class Template:
    created_at: datetime
    published_at: T.Optional[datetime] = None
    archived_at: T.Optional[datetime] = None
    id: UUID
    name: str
    recording_ids: T.Optional[T.List[UUID]] = None
    recording_name_format: T.List[str]
    is_default_template: bool = True
    description: T.Optional[str] = None
    items: T.List[TemplateItem] = field(default_factory=list)
    updated_at: datetime
    label_ids: T.List[UUID] = field(default_factory=list, metadata={"readonly": True})

    def validate_item(self, key: UUID, value: str):
        item = next((item for item in self.items if str(item.id) == key), None)
        if not item:
            raise ValueError(f"No item found with ID {key}, to define {value}.")
        else:
            item.validate_value(key, value)
