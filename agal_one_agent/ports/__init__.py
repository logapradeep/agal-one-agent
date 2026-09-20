"""Node linking on the node (ADR-024, contracts v1.9.0 — Agal/contracts/nodes/README.md).

A board is DATA in the cloud; nothing here names a board. The agent does three
things with hardware, the same way on every Linux board:

* ``gpiochip``  — drives and reads GPIO lines through the kernel's character
  device, addressed by the chip's LABEL and a line offset (chip numbering
  differs between OS images; Broadcom pin numbers exist on one board family).
* ``inventory`` — says what the board IS (model, serial, OS …) and what it HAS
  (GPIO chips with the lines the kernel holds, I2C / SPI / serial / RTC / ADC
  devices, what answers on I2C). The cloud checks a port table against this
  report, not against a list of boards.
* ``port_test`` / ``bus_scan`` — the commissioning check of one port, and the
  scan that lets the app OFFER what a bus device might be.
"""
