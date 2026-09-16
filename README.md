# Stove Controller

A custom Home Assistant integration that prevents short-cycling of a pellet stove
(or similar heating appliance) by enforcing configurable minimum on/off durations
between demand changes and relay toggling.

## Features

- Configurable minimum on and off durations (anti short-cycling protection)
- Demand switch entity acting as the source of truth for heating demand
- Controller sensor exposing state machine state (idle, heating, pending on/off)
- Configurable periodic update interval
- Home Assistant event-bus communication between switch and sensor entities
- Health check for relay entity availability

## Installation

Install via [HACS](https://hacs.xyz) by adding this repository as a custom repository
with type **Integration**.

Alternatively, copy the integration files into your `custom_components/stove_controller/`
directory and restart Home Assistant.

## Configuration

Add the integration via **Settings > Devices & Services > Add Integration** and configure:

- Relay entity (the switch that controls the stove)
- Minimum on duration (minutes)
- Minimum off duration (minutes)
- Update interval (seconds)

## License

This project is licensed under the [MIT License](LICENSE).
