"""YAML configuration loader for Menvayal Agent."""

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
class TelemetryConfig:
    interval_seconds: int = 10
    heartbeat_seconds: int = 30
    # HTTP telemetry/status ingress endpoint, supplied by the backend-generated
    # config.yaml. Making this config-driven means a backend region move needs
    # no daemon release. Empty -> the http_reporter module default is used.
    ingress_url: str = ""


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
class LoRaConfig:
    """Unified LoRa config — role determines which sub-config is used."""
    role: str = "none"  # "gateway", "end_device", or "none"
    gateway: Optional[LoRaGatewayConfig] = None
    device: Optional[LoRaDeviceConfig] = None


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

    def update_pins(self, pins_data: list[dict], config_path: str = "/etc/menvayal/config.yaml") -> None:
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
            commands_topic=topics.get("commands", f"menvayal/{node.uid}/commands"),
            telemetry_topic=topics.get("telemetry", f"menvayal/{node.uid}/telemetry"),
            status_topic=topics.get("status", f"menvayal/{node.uid}/status"),
        )

        tel_data = data.get("telemetry", {})
        telemetry = TelemetryConfig(
            interval_seconds=tel_data.get("interval_seconds", 10),
            heartbeat_seconds=tel_data.get("heartbeat_seconds", 30),
            ingress_url=tel_data.get("ingress_url", ""),
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

            lora = LoRaConfig(role=role, gateway=gw_config, device=dev_config)

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
            )
            for p in pins_data
        ]

        return cls(
            node=node, mqtt=mqtt, telemetry=telemetry, board=board,
            connectivity=connectivity, wifi=wifi, cellular=cellular,
            lora=lora, pins=pins,
        )
