# Kilowattlas Solar Sharing — Home Assistant integration

Share your rooftop solar production with [Kilowattlas](https://kilowattlas.com).
The integration reads your existing solar **power** sensor, averages it into
15-minute slots, and pushes those to Kilowattlas. Data is buffered locally, so a
network outage or Home Assistant restart never loses readings.

## Requirements

- A Kilowattlas account (sign in at https://kilowattlas.com).
- A Home Assistant **power** sensor reporting your solar output in W, kW, or MW
  (e.g. from SolarEdge, Huawei, SMA, Fronius, a Shelly EM, or a P1/HAN meter).
  An energy-only (kWh) sensor is **not** supported yet.

## Install (via HACS)

1. In HACS → **Integrations** → ⋮ → **Custom repositories**, add this repo's URL
   with category **Integration**.
2. Install **Kilowattlas Solar Sharing** and restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → Kilowattlas Solar Sharing**.

## Linking

The setup wizard uses a device-linking flow (no tokens to copy):

1. Home Assistant shows you a short **code** and a link to `kilowattlas.com/link`.
2. Open the link, sign in, and enter the code. Your site's position is prefilled
   from Home Assistant — adjust it if needed, add the panel capacity, and approve.
3. Back in Home Assistant, pick your solar power sensor. Done.

From then on the integration pushes a 15-minute average of your production once
per hour. You can see your own data on the Kilowattlas website.

## Privacy

Only 15-minute average power (kW), the slot timestamp, and the sample count are
sent — tied to the site you registered, under your account. You can revoke the
link anytime by removing the integration and deleting the site on the website.

## How it works

- Samples the sensor every ~10 s and averages each raw reading into its 15-min
  UTC slot (matching Kilowattlas's internal resolution).
- Flushes completed slots hourly, batched, to `POST /api/v1/contrib/solar`.
- Unsent slots persist across restarts; the server upsert makes resends
  idempotent, so nothing is duplicated or lost.
