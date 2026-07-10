# Graph Report - .  (2026-07-10)

## Corpus Check
- 22 files · ~74,484 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 358 nodes · 689 edges · 16 communities (15 shown, 1 thin omitted)
- Extraction: 95% EXTRACTED · 5% INFERRED · 0% AMBIGUOUS · INFERRED: 33 edges (avg confidence: 0.78)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Longitudinal Analytics & Grade Adjustment|Longitudinal Analytics & Grade Adjustment]]
- [[_COMMUNITY_Hub Generation & Run Building|Hub Generation & Run Building]]
- [[_COMMUNITY_Segment Detection Core|Segment Detection Core]]
- [[_COMMUNITY_Goal Dashboards & Race Predictions|Goal Dashboards & Race Predictions]]
- [[_COMMUNITY_Strava Export Compilation|Strava Export Compilation]]
- [[_COMMUNITY_Segment Naming & Geocoding|Segment Naming & Geocoding]]
- [[_COMMUNITY_Road Snapping & Map Matching|Road Snapping & Map Matching]]
- [[_COMMUNITY_Loop Detection|Loop Detection]]
- [[_COMMUNITY_Loop Clustering & Dedupe|Loop Clustering & Dedupe]]
- [[_COMMUNITY_Anchor Span Matching|Anchor Span Matching]]
- [[_COMMUNITY_Config & Anchored Segments|Config & Anchored Segments]]
- [[_COMMUNITY_Loop Canonicalization|Loop Canonicalization]]
- [[_COMMUNITY_Loop Geometry & Medoid|Loop Geometry & Medoid]]
- [[_COMMUNITY_Track Densification|Track Densification]]
- [[_COMMUNITY_Location Cache|Location Cache]]
- [[_COMMUNITY_Pipeline Orchestration|Pipeline Orchestration]]

## God Nodes (most connected - your core abstractions)
1. `build_segments()` - 29 edges
2. `_haversine_m()` - 24 edges
3. `generate()` - 20 edges
4. `_match_segments()` - 20 edges
5. `generate_goal_dashboard()` - 18 edges
6. `_segment_from_efforts()` - 17 edges
7. `_build_runs()` - 15 edges
8. `overview_sections()` - 12 edges
9. `ga_time()` - 12 edges
10. `_anchored_segment()` - 11 edges

## Surprising Connections (you probably didn't know these)
- `_compute_threshold()` --calls--> `robust_threshold_mps()`  [INFERRED]
  generate_hub.py → lib/generate_analytics.py
- `_build_runs()` --calls--> `fmt_pace()`  [INFERRED]
  generate_hub.py → lib/generate_dashboards.py
- `_build_runs()` --calls--> `is_interval()`  [INFERRED]
  generate_hub.py → lib/generate_dashboards.py
- `_build_runs()` --calls--> `ga_time()`  [INFERRED]
  generate_hub.py → lib/grade.py
- `_overview_stats()` --calls--> `ctl_atl_tsb()`  [INFERRED]
  generate_hub.py → lib/generate_analytics.py

## Import Cycles
- 1-file cycle: `generate_hub.py -> generate_hub.py`
- 1-file cycle: `lib/generate_analytics.py -> lib/generate_analytics.py`

## Communities (16 total, 1 thin omitted)

### Community 0 - "Longitudinal Analytics & Grade Adjustment"
Cohesion: 0.06
Nodes (53): acwr_series(), _best_5k_threshold_mps(), best_effort_points(), body_analytics(), calendar_data(), ctl_atl_tsb(), daily_series(), daniels_vo2max() (+45 more)

### Community 1 - "Hub Generation & Run Building"
Cohesion: 0.06
Nodes (53): _build_runs(), _build_threshold_curve(), _classify_run(), _compute_pace_zones(), _compute_threshold(), _detect_pauses(), generate(), _haversine_km() (+45 more)

### Community 2 - "Segment Detection Core"
Cohesion: 0.06
Nodes (41): _build_point_grid(), build_segments(), _cell_factory(), _cell_sequence(), _chain_length_m(), _classify(), _collect_efforts(), _debug_loop_cluster() (+33 more)

### Community 3 - "Goal Dashboards & Race Predictions"
Cohesion: 0.11
Nodes (36): body_best_efforts(), body_goal_dashboard(), derive_training_target(), fit_riegel(), fmt_date(), fmt_pace(), fmt_pace_bare(), fmt_pace_from_s_per_km() (+28 more)

### Community 4 - "Strava Export Compilation"
Cohesion: 0.10
Nodes (36): _best_efforts(), decompress_fit_gz(), _detect_reps(), _distance_stream(), extract_activity_id(), find_activities_csv(), find_strava_export(), _fit_keys_from() (+28 more)

### Community 5 - "Segment Naming & Geocoding"
Cohesion: 0.14
Nodes (21): _apply_stars(), body_segments(), _derive_name(), _esc(), _fallback_name(), _geo_key(), _key_centroid(), _load_json() (+13 more)

### Community 6 - "Road Snapping & Map Matching"
Cohesion: 0.10
Nodes (23): _bbox_span_ok(), _build_walk_graph(), _graph_edges_latlon(), _index_edges(), _match_polyline(), _match_segments(), _overpass_ways(), _polyline_deviation() (+15 more)

### Community 7 - "Loop Detection"
Cohesion: 0.13
Nodes (18): _dominant_laps(), _loop_centroid(), _loop_instances(), _loop_record(), _loop_roundness(), _match_run(), _neighbours(), _net_gain() (+10 more)

### Community 8 - "Loop Clustering & Dedupe"
Cohesion: 0.15
Nodes (16): _cluster_loops(), _dedupe(), _group_loop_candidates(), _haversine_m(), _is_reverse_corridor(), _nearest_match(), _polyline_len(), Cluster genuine loop candidates by ground centroid so each physical loop locatio (+8 more)

### Community 9 - "Anchor Span Matching"
Cohesion: 0.15
Nodes (13): _anchor_eligible_runs(), _anchored_segment(), _derive_anchor_line(), _match_anchor_spans(), The most typical run instance near the anchor's centre and length becomes its li, Even ~`step` m spacing along a list of {lat,lon} points -> [(lat,lon), ...]., All distance-bounded windows of one run's track that cover >= ANCHOR_COVER of th, Build an effort record (compatible with _segment_from_efforts) from a track span (+5 more)

### Community 10 - "Config & Anchored Segments"
Cohesion: 0.24
Nodes (11): Config, load_config(), _parse_anchor(), _parse_race(), Path, Race, Load config.json if present. Never raises: a missing or malformed file     yield, SegmentAnchor (+3 more)

### Community 11 - "Loop Canonicalization"
Cohesion: 0.25
Nodes (8): _absorb_efforts(), _auto_anchor_segments(), _canonicalize_loops(), _poly_centroid(), Recover loops that self-cross in too few runs to be mined as closed loops (e.g., Pool a dropped duplicate loop's efforts into the kept segment for any run it add, Collapse loops at the same place to a single canonical lap. Loops whose centroid, _span_days()

### Community 12 - "Loop Geometry & Medoid"
Cohesion: 0.25
Nodes (8): _best_roll(), _ll_to_xy(), _medoid_index(), Resample a closed loop to n points spaced equally by arc length., Cyclic offset of eff that best lines it up with ref (handles differing start poi, Index of the most representative effort: the one whose loop shape is closest to, _resample_closed(), _signed_area()

### Community 13 - "Track Densification"
Cohesion: 0.50
Nodes (4): _densify(), _geo_track(), Ordered list of dicts {lat, lon, d, t, elev} from a run's distance stream., Insert interpolated points so consecutive samples are <= DENSIFY_M apart.     K

### Community 14 - "Location Cache"
Cohesion: 0.50
Nodes (4): _located_cache(), _located_names(), (centroid, value) for every cached entry, so a result survives small polyline ji, (centroid, name) for every cached name entry (see _located_cache).

## Knowledge Gaps
- **2 isolated node(s):** `Path`, `Path`
  These have ≤1 connection - possible missing edges or undocumented components.
- **1 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `_segment_from_efforts()` connect `Segment Detection Core` to `Longitudinal Analytics & Grade Adjustment`, `Goal Dashboards & Race Predictions`, `Segment Naming & Geocoding`, `Loop Detection`, `Loop Clustering & Dedupe`, `Anchor Span Matching`, `Loop Canonicalization`, `Loop Geometry & Medoid`?**
  _High betweenness centrality (0.200) - this node is a cross-community bridge._
- **Why does `main()` connect `Hub Generation & Run Building` to `Longitudinal Analytics & Grade Adjustment`, `Segment Detection Core`, `Goal Dashboards & Race Predictions`, `Segment Naming & Geocoding`?**
  _High betweenness centrality (0.179) - this node is a cross-community bridge._
- **Why does `ga_time()` connect `Longitudinal Analytics & Grade Adjustment` to `Hub Generation & Run Building`, `Segment Detection Core`, `Goal Dashboards & Race Predictions`?**
  _High betweenness centrality (0.174) - this node is a cross-community bridge._
- **Are the 2 inferred relationships involving `generate_goal_dashboard()` (e.g. with `load_config()` and `ga_time()`) actually correct?**
  _`generate_goal_dashboard()` has 2 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Insert intermediate lat/lon points so no segment is longer than step_m metres.`, `Build heatmap points where each run contributes at most one point per ~100 m gri`, `Derive threshold speed (m/s) from a multi-point, grade-adjusted fitness curve` to the rest of the system?**
  _138 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Longitudinal Analytics & Grade Adjustment` be split into smaller, more focused modules?**
  _Cohesion score 0.05701754385964912 - nodes in this community are weakly interconnected._
- **Should `Hub Generation & Run Building` be split into smaller, more focused modules?**
  _Cohesion score 0.06219426974143955 - nodes in this community are weakly interconnected._