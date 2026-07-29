"""Analytics marts as DuckDB views over the Parquet layers.

Each mart declares the tables it needs. A mart whose dependencies have no data yet is skipped
rather than created broken, so a partially-populated warehouse still answers what it can.

Naming is deliberate: anything that cannot be computed honestly from public data says so in
its name. `mart_punctuality_proxy` is a proxy because no free source publishes scheduled
times - it measures observed movements against a rolling baseline, not against a timetable.

Every ``to_timestamp()`` is cast to a naive ``TIMESTAMP``. All epochs in this warehouse are
UTC, and leaving them as ``TIMESTAMPTZ`` would both drag in a pytz dependency on fetch and
let the session timezone silently shift day boundaries in the date-grouped marts.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Mart:
    name: str
    depends_on: tuple[str, ...]
    sql: str
    description: str


MARTS: tuple[Mart, ...] = (
    Mart(
        "mart_traffic_density",
        ("fct_position",),
        """
        SELECT hub,
               date_trunc('hour', to_timestamp(snapshot_time)::TIMESTAMP) AS hour,
               COUNT(DISTINCT hex)                             AS aircraft,
               COUNT(*)                                        AS fixes,
               COUNT(DISTINCT hex) FILTER (WHERE on_ground)    AS on_ground,
               ROUND(AVG(alt_baro) FILTER (WHERE NOT on_ground))      AS avg_alt_ft,
               ROUND(AVG(ground_speed) FILTER (WHERE NOT on_ground))  AS avg_gs_kt,
               COUNT(DISTINCT aircraft_type)                   AS distinct_types,
               COUNT(DISTINCT source)                          AS feeds_used
        FROM fct_position
        GROUP BY 1, 2
        """,
        "Traffic volume and profile per hub per hour",
    ),
    Mart(
        "mart_fleet_utilisation",
        ("fct_flight_leg",),
        """
        SELECT registration,
               aircraft_type,
               CAST(to_timestamp(start_time)::TIMESTAMP AS DATE) AS day,
               COUNT(*)                               AS legs,
               ROUND(SUM(duration_s) / 3600.0, 2)     AS airborne_hours,
               ROUND(SUM(track_distance_nm))          AS distance_nm,
               COUNT(*) FILTER (WHERE complete)       AS complete_legs
        FROM fct_flight_leg
        WHERE registration IS NOT NULL
        GROUP BY 1, 2, 3
        """,
        "Legs, hours and distance per tail per day",
    ),
    Mart(
        "mart_route_efficiency",
        ("fct_flight_leg",),
        """
        SELECT aircraft_type,
               COUNT(*)                          AS legs,
               ROUND(AVG(track_efficiency), 3)   AS avg_track_ratio,
               ROUND(MAX(track_efficiency), 3)   AS worst_track_ratio,
               ROUND(AVG(track_distance_nm))     AS avg_flown_nm,
               ROUND(AVG(direct_distance_nm))    AS avg_direct_nm,
               -- Excess distance is the fuel-burn proxy: every extra mile is burnt fuel.
               ROUND(AVG(track_distance_nm - direct_distance_nm)) AS avg_excess_nm
        FROM fct_flight_leg
        WHERE track_efficiency IS NOT NULL
        GROUP BY 1
        HAVING COUNT(*) >= 3
        """,
        "Flown vs direct distance by type - excess miles proxy fuel burn",
    ),
    Mart(
        "mart_weather_risk",
        ("fct_weather_obs",),
        """
        SELECT station,
               date_trunc('hour', to_timestamp(obs_time)::TIMESTAMP) AS hour,
               ANY_VALUE(flight_category)                 AS flight_category,
               MIN(visibility_sm)                         AS min_visibility_sm,
               MIN(ceiling_ft)                            AS min_ceiling_ft,
               MAX(wind_speed_kt)                         AS max_wind_kt,
               MAX(COALESCE(wind_gust_kt, 0))             AS max_gust_kt,
               -- IFR/LIFR is when approach minima and diversion planning start to bite.
               BOOL_OR(flight_category IN ('IFR', 'LIFR')) AS below_minima_risk
        FROM fct_weather_obs
        GROUP BY 1, 2
        """,
        "Hourly weather exposure per station, flagged at IFR/LIFR",
    ),
    Mart(
        "mart_network_disruption",
        ("fct_disruption",),
        """
        SELECT airport,
               delay_type,
               reason,
               COUNT(*)                                   AS observations,
               MIN(to_timestamp(observed_at)::TIMESTAMP)             AS first_seen,
               MAX(to_timestamp(observed_at)::TIMESTAMP)             AS last_seen,
               ANY_VALUE(avg_delay)                       AS sample_avg_delay,
               ANY_VALUE(max_delay)                       AS sample_max_delay
        FROM fct_disruption
        GROUP BY 1, 2, 3
        """,
        "FAA ground delay programmes, stops and closures by airport and cause",
    ),
    Mart(
        "mart_safety_event",
        ("fct_position",),
        """
        SELECT hex,
               ANY_VALUE(callsign)              AS callsign,
               ANY_VALUE(registration)          AS registration,
               ANY_VALUE(aircraft_type)         AS aircraft_type,
               emergency,
               MIN(to_timestamp(snapshot_time)::TIMESTAMP) AS first_seen,
               MAX(to_timestamp(snapshot_time)::TIMESTAMP) AS last_seen,
               COUNT(*)                         AS fixes,
               ROUND(MAX(alt_baro))             AS max_alt_ft
        FROM fct_position
        WHERE emergency IS NOT NULL
        GROUP BY hex, emergency
        """,
        "Emergency squawks (7500/7600/7700) and declared emergencies",
    ),
    Mart(
        "mart_punctuality_proxy",
        ("fct_flight_leg",),
        """
        -- PROXY ONLY. No free source publishes scheduled times, so this compares each
        -- callsign's observed block time against its own rolling median rather than
        -- against a timetable. It detects degradation, not lateness.
        WITH baseline AS (
            SELECT callsign, MEDIAN(duration_s) AS typical_s, COUNT(*) AS samples
            FROM fct_flight_leg
            WHERE callsign IS NOT NULL AND complete
            GROUP BY 1
            HAVING COUNT(*) >= 3
        )
        SELECT l.callsign,
               l.registration,
               CAST(to_timestamp(l.start_time)::TIMESTAMP AS DATE)      AS day,
               l.duration_s,
               b.typical_s,
               b.samples                                     AS baseline_samples,
               ROUND((l.duration_s - b.typical_s) / 60.0, 1) AS delta_minutes
        FROM fct_flight_leg l
        JOIN baseline b USING (callsign)
        WHERE l.complete
        """,
        "Block time vs own rolling median - degradation proxy, NOT schedule adherence",
    ),
    Mart(
        "mart_launch_conflict",
        ("fct_launch_window",),
        """
        -- Latest known state per launch: rows accumulate as the NET moves, so the newest
        -- observation per launch_id is the current plan.
        WITH latest AS (
            SELECT *, ROW_NUMBER() OVER (
                       PARTITION BY launch_id ORDER BY observed_at DESC, net DESC) AS rn
            FROM fct_launch_window
        )
        SELECT launch_id, name, provider, vehicle, status,
               net, window_start, window_end,
               pad_name, pad_location, pad_latitude, pad_longitude,
               probability, weather_concerns, hold_reason
        FROM latest
        WHERE rn = 1
        """,
        "Current launch windows with pad geography - airspace closure planning",
    ),
    Mart(
        "mart_launch_slips",
        ("fct_launch_window",),
        """
        -- Slip history falls out of the key design: each distinct NET appends a row.
        SELECT launch_id,
               ANY_VALUE(name)               AS name,
               ANY_VALUE(provider)           AS provider,
               COUNT(DISTINCT net)           AS distinct_nets,
               MIN(net)                      AS earliest_net,
               MAX(net)                      AS latest_net,
               date_diff('day', CAST(MIN(net) AS TIMESTAMP), CAST(MAX(net) AS TIMESTAMP))
                                             AS slip_days,
               ANY_VALUE(weather_concerns)   AS weather_concerns
        FROM fct_launch_window
        WHERE net IS NOT NULL
        GROUP BY launch_id
        HAVING COUNT(DISTINCT net) > 1
        """,
        "Launches whose NET moved, and by how much",
    ),
    Mart(
        "mart_orbital_decay",
        ("fct_orbital_element",),
        """
        -- Mean motion rises as an object loses altitude, so its change across epochs is
        -- the decay signal.
        SELECT norad_cat_id,
               ANY_VALUE(object_name)                  AS object_name,
               ANY_VALUE(launch_designator)            AS launch_designator,
               COUNT(*)                                AS epochs,
               MIN(epoch)                              AS first_epoch,
               MAX(epoch)                              AS last_epoch,
               ROUND(MIN(period_minutes), 3)           AS min_period_min,
               ROUND(MAX(period_minutes), 3)           AS max_period_min,
               ROUND(MAX(mean_motion) - MIN(mean_motion), 6) AS mean_motion_delta,
               ROUND(AVG(bstar), 8)                    AS avg_bstar,
               ROUND(AVG(inclination), 2)              AS inclination
        FROM fct_orbital_element
        GROUP BY norad_cat_id
        """,
        "Orbital decay signal per catalogued object",
    ),
    Mart(
        "mart_fleet_identity",
        ("dim_aircraft",),
        """
        -- Current identity per airframe. Closed versions stay in dim_aircraft so historical
        -- legs keep resolving to the tail that actually flew them.
        SELECT hex, registration, aircraft_type,
               operator_icao,          -- controlled vocabulary: the grouping key
               operator,               -- free-text display name, NOT safe to group on
               to_timestamp(first_seen)::TIMESTAMP AS first_seen,
               to_timestamp(last_seen)::TIMESTAMP  AS last_seen
        FROM dim_aircraft
        WHERE is_current
        """,
        "Current tail/type/operator per airframe (SCD2 current rows)",
    ),
    Mart(
        "mart_fuel_exposure",
        ("fct_flight_leg", "fct_fuel_price"),
        """
        -- Excess miles are burnt fuel, and fuel has a price. A widebody burns roughly
        -- 6 US gal/nm and a narrowbody roughly 2.5; 3.5 is used as a fleet-mixed midpoint.
        -- This is an ORDER-OF-MAGNITUDE estimate, not an accounting figure: real burn
        -- depends on weight, altitude and winds none of which are in public data.
        WITH latest_price AS (
            SELECT price, units, period
            FROM fct_fuel_price
            WHERE price IS NOT NULL
            ORDER BY period DESC
            LIMIT 1
        )
        SELECT l.aircraft_type,
               COUNT(*)                                              AS legs,
               ROUND(SUM(l.track_distance_nm - l.direct_distance_nm)) AS excess_nm,
               ANY_VALUE(p.price)                                    AS fuel_price,
               ANY_VALUE(p.units)                                    AS price_units,
               ANY_VALUE(p.period)                                   AS price_date,
               ROUND(SUM(l.track_distance_nm - l.direct_distance_nm) * 3.5
                     * ANY_VALUE(p.price), 2)                        AS est_excess_cost
        FROM fct_flight_leg l
        CROSS JOIN latest_price p
        WHERE l.direct_distance_nm IS NOT NULL
        GROUP BY l.aircraft_type
        HAVING COUNT(*) >= 3
        """,
        "Order-of-magnitude cost of excess miles flown, at latest jet fuel spot price",
    ),
    Mart(
        "mart_notam_impact",
        ("fct_notam",),
        """
        SELECT icao_location AS airport,
               classification,
               type,
               COUNT(*) AS notams,
               MIN(effective_start) AS earliest_start,
               MAX(effective_end)   AS latest_end
        FROM fct_notam
        GROUP BY 1, 2, 3
        """,
        "Active NOTAMs per aerodrome by class",
    ),
)


def refresh(conn, available: set[str]) -> tuple[list[str], list[str]]:
    """Create every mart whose dependencies are present. Returns (created, skipped)."""
    created, skipped = [], []
    for mart in MARTS:
        if not set(mart.depends_on).issubset(available):
            skipped.append(mart.name)
            continue
        conn.execute(f"CREATE OR REPLACE VIEW {mart.name} AS {mart.sql}")
        created.append(mart.name)
    return created, skipped
