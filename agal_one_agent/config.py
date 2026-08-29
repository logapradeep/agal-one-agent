"""YAML configuration loader for Agal One Agent."""

from dataclasses import dataclass, field
from typing import Optional
import yaml


@dataclass
class NodeConfig:
    uid: str
    name: str
    auth_token: str
    node_type: str = "link_rio"


@dataclass
class MqttConfig:
    broker: str
    port: int = 8883
    tls: bool = True
    username: str = ""
    password: str = ""
    commands_topic: str = ""
    telemetry_topic: str = ""
    status_topic: str = ""


@dataclass
class TelemetryBatchConfigYaml:
    """Durable-buffer batch tunables (ADR-013 §5.7 ``telemetry.batch{...}``).

    Kept as a plain config dataclass here; the runtime buffer converts it to a
    ``telemetry_buffer.TelemetryBatchConfig`` via ``to_batch_config()``."""
    flush_interval_sec: int = 900
    max_readings: int = 500
    buffer_max_mb: int = 64
    retention_hours: int = 48

    def to_batch_config(self):
        """Bridge to the runtime ``telemetry_buffer.TelemetryBatchConfig``.
        Imported lazily so config.py has no dependency on the buffer module."""
        from .telemetry_buffer import TelemetryBatchConfig
        return TelemetryBatchConfig(
            flush_interval_sec=self.flush_interval_sec,
            max_readings=self.max_readings,
            buffer_max_mb=self.buffer_max_mb,
            retention_hours=self.retention_hours,
        )


@dataclass
class TelemetryConfig:
    interval_seconds: int = 10
    heartbeat_seconds: int = 30
    # HTTP telemetry/status ingress endpoint, supplied by the backend-generated
    # config.yaml. Making this config-driven means a backend region move needs
    # no daemon release. Empty -> the http_reporter module default is used.
    ingress_url: str = ""
    # ADR-013 P0 durable ring buffer + batch uploader tunables.
    batch: "TelemetryBatchConfigYaml" = field(default_factory=TelemetryBatchConfigYaml)
    # ADR-013 §4.4 / §9-D3 adaptive live cadence. When True the node publishes
    # the live snapshot at the fast cadence even without an app-watch signal
    # (bench/dev default). In prod this stays False and the watch signal drives
    # the switch. See live_cadence.LiveCadenceController.
    live_watch_default: bool = False
    live_idle_seconds: int = 60      # idle live cadence when nobody is watching
    # On-node durable buffer DB path (outside the git checkout so OTA preserves
    # it — ADR-013 §10 "Harder"). Empty -> telemetry_buffer.DEFAULT_DB_PATH.
    buffer_db_path: str = ""


@dataclass
class PinConfig:
    physical_pin: int
    gpio_number: Optional[int] = None
    protocol: str = "gpio_input"
    label: str = ""
    assigned_to: Optional[str] = None
    # Bus protocol metadata
    bus_id: Optional[str] = None              # e.g., "I2C1", "SPI0", "UART0"
    i2c_address: Optional[int] = None         # 7-bit I2C address (0x00-0x7F)
    i2c_register: Optional[int] = None        # Start register for read/write
    spi_cs_pin: Optional[int] = None          # CS pin number for SPI device selection
    uart_baud_rate: Optional[int] = None      # UART baud rate
    one_wire_device_id: Optional[str] = None  # 1-Wire ROM ID (e.g., "28-00000ABCDE")
    # PWM metadata (protocol == "pwm") — mirrors the contracted PortTransport
    # `pwm` variant (Agal/contracts/schemas/asset.schema.json: frequencyHz +
    # dutyCyclePct + polarity), delivered snake_cased in the syncPinConfig
    # pins payload by toDaemonPinConfig (agal-one/backend .../mqtt/client.ts).
    pwm_frequency_hz: Optional[float] = None   # Carrier frequency in Hz (default 1000 at the handler)
    pwm_duty_cycle_pct: Optional[float] = None  # Initial/on duty cycle, 0-100
    pwm_polarity: Optional[str] = None          # "normal" (default) or "inverted"
    # Sensor driver metadata (see agal_one_agent/sensors/)
    sensor_type: Optional[str] = None         # e.g., "current_acs758", "ultrasonic_jsn_sr04t"
    sensor_params: dict = field(default_factory=dict)
    # Edge protection config (dry-run cutoff, low-water warnings) — see sensors/protection.py
    protection: Optional[dict] = None


@dataclass
class BoardConfig:
    model: str = "unknown"
    category: str = "custom"
    hat_id: Optional[str] = None
    hat_name: Optional[str] = None
    hat_consumed_pins: list[int] = field(default_factory=list)


@dataclass
class WifiConfig:
    ssid: str = ""
    password: str = ""
    country_code: str = "IN"


@dataclass
class CellularConfig:
    apn: str = ""
    pin: str = ""  # SIM PIN if set


@dataclass
class LoRaGatewayConfig:
    """Config for a LoRa gateway node running packet forwarder + ChirpStack bridge."""
    gateway_eui: str = ""
    region: str = "IN865"
    # ChirpStack Gateway Bridge settings
    bridge_mqtt_broker: str = "localhost"
    bridge_mqtt_port: int = 1883
    bridge_topic_prefix: str = "gateway"
    # Semtech UDP packet forwarder settings
    pkt_fwd_server: str = "localhost"
    pkt_fwd_port_up: int = 1700
    pkt_fwd_port_down: int = 1700


@dataclass
class LoRaDeviceConfig:
    """Config for a LoRa end-device registered on a parent gateway."""
    dev_eui: str = ""
    app_key: str = ""
    join_eui: str = "0000000000000000"
    join_method: str = "OTAA"  # OTAA or ABP
    parent_gateway_uid: str = ""
    # ABP-only fields
    dev_addr: Optional[str] = None
    nwk_s_key: Optional[str] = None
    app_s_key: Optional[str] = None


@dataclass
class LoRaRadioConfig:
    """SX127x radio parameters for the raw-star hub (ADR-011 §6 / §10.5)."""
    chipset: str = "sx1276"
    freq_mhz: float = 865.985       # IN865 default channel
    sf: int = 9                     # network-fixed spreading factor
    bw_khz: int = 125
    tx_dbm: int = 14                # <= +14 dBm (25 mW), inside WPC limits
    # SPI wiring (RPi 40-pin header; DIO0 → GPIO IRQ). Live path only.
    spi_bus: int = 0
    spi_device: int = 0
    dio0_gpio: Optional[int] = None


@dataclass
class LoRaConfig:
    """Unified LoRa config — ``mode``/``role`` determine which path is used.

    v1 (ADR-011): ``mode == "raw_star"`` is the active raw-LoRa gateway path
    (:mod:`lora_listener`), gated behind ``enabled`` (default False ⇒ DARK). The
    legacy ``role == "gateway"/"end_device"`` fields drive the dormant LoRaWAN
    bridge (:mod:`lora_bridge`), retained as the P4 scale-out seam.
    """
    role: str = "none"  # LoRaWAN bridge role: "gateway", "end_device", or "none"
    gateway: Optional[LoRaGatewayConfig] = None
    device: Optional[LoRaDeviceConfig] = None
    # --- Raw-star (v1) fields ---
    mode: str = "none"              # "raw_star" | "lorawan" | "none"
    enabled: bool = False           # DARK by default (no bench hardware yet)
    region: str = "IN865"
    farm_key_id: str = ""           # K_farm VERSION only; key lives in Secret Manager
    radio: Optional["LoRaRadioConfig"] = None


@dataclass
class AgentConfig:
    node: NodeConfig
    mqtt: MqttConfig
    telemetry: TelemetryConfig
    board: BoardConfig
    connectivity: str = "WiFi"
    wifi: Optional[WifiConfig] = None
    cellular: Optional[CellularConfig] = None
    lora: Optional[LoRaConfig] = None
    pins: list[PinConfig] = field(default_factory=list)

    @property
    def is_lora_gateway(self) -> bool:
        return self.lora is not None and self.lora.role == "gateway"

    @property
    def is_lora_device(self) -> bool:
        return self.lora is not None and self.lora.role == "end_device"

    @property
    def is_lora_raw_star(self) -> bool:
        """True when the v1 raw-LoRa star gateway path is configured AND enabled.
        The listener additionally gates on spidev availability, so this being
        True does not by itself touch hardware (ADR-011 §10 DARK-by-default)."""
        return (self.lora is not None
                and self.lora.mode == "raw_star"
                and self.lora.enabled)

    def update_pins(self, pins_data: list[dict], config_path: str = "/etc/agal-one-agent/config.yaml") -> None:
        """Update pin configuration in memory and persist to config.yaml."""
        self.pins = [
            PinConfig(
                physical_pin=p["physical_pin"],
                gpio_number=p.get("gpio_number"),
                protocol=p.get("protocol", "gpio_input"),
                label=p.get("label", ""),
                assigned_to=p.get("assigned_to"),
                bus_id=p.get("bus_id"),
                i2c_address=p.get("i2c_address"),
                i2c_register=p.get("i2c_register"),
                spi_cs_pin=p.get("spi_cs_pin"),
                uart_baud_rate=p.get("uart_baud_rate"),
                one_wire_device_id=p.get("one_wire_device_id"),
                pwm_frequency_hz=p.get("pwm_frequency_hz"),
                pwm_duty_cycle_pct=p.get("pwm_duty_cycle_pct"),
                pwm_polarity=p.get("pwm_polarity"),
                sensor_type=p.get("sensor_type"),
                sensor_params=p.get("sensor_params") or {},
                protection=p.get("protection"),
            )
            for p in pins_data
        ]

        # Persist to config.yaml
        try:
            with open(config_path, "r") as f:
                data = yaml.safe_load(f) or {}

            # Build clean pins list for YAML
            yaml_pins = []
            for p in pins_data:
                entry: dict = {
                    "physical_pin": p["physical_pin"],
                }
                if p.get("gpio_number") is not None:
                    entry["gpio_number"] = p["gpio_number"]
                if p.get("protocol"):
                    entry["protocol"] = p["protocol"]
                if p.get("label"):
                    entry["label"] = p["label"]
                if p.get("assigned_to"):
                    entry["assigned_to"] = p["assigned_to"]
                if p.get("bus_id"):
                    entry["bus_id"] = p["bus_id"]
                if p.get("i2c_address") is not None:
                    entry["i2c_address"] = p["i2c_address"]
                if p.get("i2c_register") is not None:
                    entry["i2c_register"] = p["i2c_register"]
                if p.get("spi_cs_pin") is not None:
                    entry["spi_cs_pin"] = p["spi_cs_pin"]
                if p.get("uart_baud_rate") is not None:
                    entry["uart_baud_rate"] = p["uart_baud_rate"]
                if p.get("one_wire_device_id"):
                    entry["one_wire_device_id"] = p["one_wire_device_id"]
                if p.get("pwm_frequency_hz") is not None:
                    entry["pwm_frequency_hz"] = p["pwm_frequency_hz"]
                if p.get("pwm_duty_cycle_pct") is not None:
                    entry["pwm_duty_cycle_pct"] = p["pwm_duty_cycle_pct"]
                if p.get("pwm_polarity"):
                    entry["pwm_polarity"] = p["pwm_polarity"]
                if p.get("sensor_type"):
                    entry["sensor_type"] = p["sensor_type"]
                if p.get("sensor_params"):
                    entry["sensor_params"] = p["sensor_params"]
                if p.get("protection"):
                    entry["protection"] = p["protection"]
                yaml_pins.append(entry)

            data["pins"] = yaml_pins

            with open(config_path, "w") as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False)

        except Exception as e:
            import logging
            logging.getLogger(__name__).error("Failed to persist pin config: %s", e)

    @classmethod
    def from_yaml(cls, path: str) -> "AgentConfig":
        with open(path, "r") as f:
            data = yaml.safe_load(f)

        node_data = data.get("node", {})
        node = NodeConfig(
            uid=node_data["uid"],
            name=node_data.get("name", ""),
            auth_token=node_data["auth_token"],
            node_type=node_data.get("type", "link_rio"),
        )

        mqtt_data = data.get("mqtt", {})
        topics = mqtt_data.get("topics", {})
        mqtt = MqttConfig(
            broker=mqtt_data["broker"],
            port=mqtt_data.get("port", 8883),
            tls=mqtt_data.get("tls", True),
            username=mqtt_data.get("username", node.uid),
            password=mqtt_data.get("password", node.auth_token),
            commands_topic=topics.get("commands", f"agal/{node.uid}/commands"),
            telemetry_topic=topics.get("telemetry", f"agal/{node.uid}/telemetry"),
            status_topic=topics.get("status", f"agal/{node.uid}/status"),
        )

        tel_data = data.get("telemetry", {})
        batch_data = tel_data.get("batch", {}) or {}
        telemetry = TelemetryConfig(
            interval_seconds=tel_data.get("interval_seconds", 10),
            heartbeat_seconds=tel_data.get("heartbeat_seconds", 30),
            ingress_url=tel_data.get("ingress_url", ""),
            batch=TelemetryBatchConfigYaml(
                flush_interval_sec=batch_data.get("flush_interval_sec", 900),
                max_readings=batch_data.get("max_readings", 500),
                buffer_max_mb=batch_data.get("buffer_max_mb", 64),
                retention_hours=batch_data.get("retention_hours", 48),
            ),
            live_watch_default=tel_data.get("live_watch_default", False),
            live_idle_seconds=tel_data.get("live_idle_seconds", 60),
            buffer_db_path=tel_data.get("buffer_db_path", ""),
        )

        board_data = data.get("board", {})
        hat_data = board_data.get("hat", {})
        board = BoardConfig(
            model=board_data.get("model", "unknown"),
            category=board_data.get("category", "custom"),
            hat_id=hat_data.get("id"),
            hat_name=hat_data.get("name"),
            hat_consumed_pins=hat_data.get("consumed_pins", []),
        )

        connectivity = data.get("connectivity", "WiFi")

        # WiFi config
        wifi = None
        wifi_data = data.get("wifi")
        if wifi_data:
            wifi = WifiConfig(
                ssid=wifi_data.get("ssid", ""),
                password=wifi_data.get("password", ""),
                country_code=wifi_data.get("country_code", "IN"),
            )

        # Cellular config
        cellular = None
        cellular_data = data.get("cellular")
        if cellular_data:
            cellular = CellularConfig(
                apn=cellular_data.get("apn", ""),
                pin=cellular_data.get("pin", ""),
            )

        # LoRa config
        lora = None
        lora_data = data.get("lora")
        if lora_data:
            role = lora_data.get("role", "none")
            gw_config = None
            dev_config = None

            if role == "gateway":
                gw_data = lora_data.get("gateway", {})
                gw_config = LoRaGatewayConfig(
                    gateway_eui=gw_data.get("gateway_eui", ""),
                    region=gw_data.get("region", "IN865"),
                    bridge_mqtt_broker=gw_data.get("bridge_mqtt_broker", "localhost"),
                    bridge_mqtt_port=gw_data.get("bridge_mqtt_port", 1883),
                    bridge_topic_prefix=gw_data.get("bridge_topic_prefix", "gateway"),
                    pkt_fwd_server=gw_data.get("pkt_fwd_server", "localhost"),
                    pkt_fwd_port_up=gw_data.get("pkt_fwd_port_up", 1700),
                    pkt_fwd_port_down=gw_data.get("pkt_fwd_port_down", 1700),
                )
            elif role == "end_device":
                dev_data = lora_data.get("device", {})
                dev_config = LoRaDeviceConfig(
                    dev_eui=dev_data.get("dev_eui", ""),
                    app_key=dev_data.get("app_key", ""),
                    join_eui=dev_data.get("join_eui", "0000000000000000"),
                    join_method=dev_data.get("join_method", "OTAA"),
                    parent_gateway_uid=dev_data.get("parent_gateway_uid", ""),
                    dev_addr=dev_data.get("dev_addr"),
                    nwk_s_key=dev_data.get("nwk_s_key"),
                    app_s_key=dev_data.get("app_s_key"),
                )

            # Raw-star (v1) block — DARK unless mode == "raw_star" AND enabled.
            radio_cfg = None
            radio_data = lora_data.get("radio")
            if radio_data:
                radio_cfg = LoRaRadioConfig(
                    chipset=radio_data.get("chipset", "sx1276"),
                    freq_mhz=radio_data.get("freq_mhz", 865.985),
                    sf=radio_data.get("sf", 9),
                    bw_khz=radio_data.get("bw_khz", 125),
                    tx_dbm=radio_data.get("tx_dbm", 14),
                    spi_bus=radio_data.get("spi_bus", 0),
                    spi_device=radio_data.get("spi_device", 0),
                    dio0_gpio=radio_data.get("dio0_gpio"),
                )

            lora = LoRaConfig(
                role=role, gateway=gw_config, device=dev_config,
                mode=lora_data.get("mode", "none"),
                enabled=bool(lora_data.get("enabled", False)),
                region=lora_data.get("region", "IN865"),
                farm_key_id=lora_data.get("farm_key_id", ""),
                radio=radio_cfg,
            )

        pins_data = data.get("pins", []) or []
        pins = [
            PinConfig(
                physical_pin=p["physical_pin"],
                gpio_number=p.get("gpio_number"),
                protocol=p.get("protocol", "gpio_input"),
                label=p.get("label", ""),
                assigned_to=p.get("assigned_to"),
                bus_id=p.get("bus_id"),
                i2c_address=p.get("i2c_address"),
                i2c_register=p.get("i2c_register"),
                spi_cs_pin=p.get("spi_cs_pin"),
                uart_baud_rate=p.get("uart_baud_rate"),
                one_wire_device_id=p.get("one_wire_device_id"),
                pwm_frequency_hz=p.get("pwm_frequency_hz"),
                pwm_duty_cycle_pct=p.get("pwm_duty_cycle_pct"),
                pwm_polarity=p.get("pwm_polarity"),
                sensor_type=p.get("sensor_type"),
                sensor_params=p.get("sensor_params") or {},
                protection=p.get("protection"),
            )
            for p in pins_data
        ]

        return cls(
            node=node, mqtt=mqtt, telemetry=telemetry, board=board,
            connectivity=connectivity, wifi=wifi, cellular=cellular,
            lora=lora, pins=pins,
        )
