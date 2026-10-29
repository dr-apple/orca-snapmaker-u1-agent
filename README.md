# Snapmaker U1 printer agent for OrcaSlicer 2.5

A Python printer-agent plugin that connects the **Snapmaker U1** to **OrcaSlicer 2.5** over LAN (Moonraker). It does what the built-in Snapmaker agent does not do yet:

- **Print options:** timelapse, flow calibration, bed leveling, and the filament-to-slot mapping are sent to the printer. It uses the U1's own `server.files.start_local_print`, the same request Snapmaker Orca sends. See [OrcaSlicer#16836](https://github.com/OrcaSlicer/OrcaSlicer/issues/16836).
- **Filament sync:** all four slots appear as one AMS unit. On 2.5.0-beta and later, each synced slot then gets its best preset by vendor, type, sub type and colour (e.g. *Snapmaker PLA SnapSpeed @U1*, *Snapmaker PETG HF @U1*).
- **Status and camera:** state, temperatures, progress and storage, plus the U1's cameras as snapshots.
- **U1 tab:** a main-window tab with camera, job, the four toolheads, motion and filament slots.

Deutsche Anleitung: [README.de.md](README.de.md)

## Requirements

- OrcaSlicer **2.5.0-beta** (2.5.0-alpha also works, but without automatic preset assignment). Tested on macOS (Apple Silicon).
- A U1 reachable on your network, with Moonraker on port 7125 and no API key (the U1 default). Tested with firmware 2.0.0 (paxx12).

## Install

1. **Preferences → Developer → Experimental Features:** enable *Use printer agents instead of print hosts*.
2. **Plugins → Install local plugin:** pick `u1_moonraker_agent.py`.
3. In the Plugins dialog, enable *Snapmaker U1 Agent* and both of its capabilities, *Snapmaker U1 (Moonraker)* and *U1*.
4. Restart OrcaSlicer.

## Set up the printer preset

1. Select your U1 printer preset and click **Edit**. Switch on **Expert mode**.
2. **Basic information → Advanced:**
   - **Print host:** the U1's IP address.
   - **Printer agent:** *Snapmaker U1 (Moonraker)*.
3. Save the preset.

Open the **Device** tab and allow the plugin to start threads when asked.

## Filaments

- Press the **sync button** in the filament area. On the beta, each slot then gets its matching preset. The **Assign presets** button (*Profile zuweisen*) in the U1 tab does the same without re-syncing.
- A preset can only be assigned when it is enabled in OrcaSlicer's filament selection. Enable the presets of the filaments you use, e.g. *Snapmaker PLA Basic @U1*. Otherwise the closest enabled preset is used.
- A slot is only reassigned to a preset of the type it already holds. If a filament preset has unsaved changes, OrcaSlicer asks first, as usual.

## Known limits

- The **Device** tab shows one extruder. OrcaSlicer 2.5 draws at most two, and reporting four crashes it. All four toolheads are in the U1 tab.
- The **printer sync** button cannot match the U1's four nozzles. It keeps your printer preset.
- The camera shows snapshots (about 5 per second), not video.
- The plugin's UI text is German.

## Logs

- Plugin: `~/Library/Application Support/OrcaSlicer/log/python_*.log`. Lines start with `[u1_moonraker]`.
- OrcaSlicer: `debug_*.log.0` in the same folder.

## License

MIT, see [LICENSE](LICENSE). This is not an official Snapmaker or OrcaSlicer project.
