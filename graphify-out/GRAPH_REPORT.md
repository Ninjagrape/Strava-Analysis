# Graph Report - C:\Users\LeeMo\Documents\GitHub\Strava-Analysis  (2026-07-09)

## Corpus Check
- 22 files · ~74,247 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 439 nodes · 852 edges · 19 communities (18 shown, 1 thin omitted)
- Extraction: 93% EXTRACTED · 7% INFERRED · 0% AMBIGUOUS · INFERRED: 57 edges (avg confidence: 0.8)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Analytics & Cache Concepts|Analytics & Cache Concepts]]
- [[_COMMUNITY_Analytics Computation Module|Analytics Computation Module]]
- [[_COMMUNITY_Best-Efforts & Goals Module|Best-Efforts & Goals Module]]
- [[_COMMUNITY_Hub Generation & Classification|Hub Generation & Classification]]
- [[_COMMUNITY_Fit Parsing & Compile|Fit Parsing & Compile]]
- [[_COMMUNITY_Segment Naming & Road Snapping|Segment Naming & Road Snapping]]
- [[_COMMUNITY_Corridor Mining & Cells|Corridor Mining & Cells]]
- [[_COMMUNITY_Segment Dedupe & Anchoring|Segment Dedupe & Anchoring]]
- [[_COMMUNITY_Effort Matching & Climbs|Effort Matching & Climbs]]
- [[_COMMUNITY_Segment Classification & Elevation|Segment Classification & Elevation]]
- [[_COMMUNITY_Anchored Loop Segments|Anchored Loop Segments]]
- [[_COMMUNITY_Loop Candidate Detection|Loop Candidate Detection]]
- [[_COMMUNITY_Config Loading|Config Loading]]
- [[_COMMUNITY_Loop Geometry & Medoid|Loop Geometry & Medoid]]
- [[_COMMUNITY_Loop Clustering|Loop Clustering]]
- [[_COMMUNITY_Reverse-Geocode Naming|Reverse-Geocode Naming]]
- [[_COMMUNITY_Loop Fragment Filtering|Loop Fragment Filtering]]
- [[_COMMUNITY_Geo-Track Densification|Geo-Track Densification]]
- [[_COMMUNITY_Pipeline Entrypoint|Pipeline Entrypoint]]

## God Nodes (most connected - your core abstractions)
1. `build_segments()` - 29 edges
2. `_haversine_m()` - 24 edges
3. `project-map skill` - 21 edges
4. `generate_goal_dashboard()` - 20 edges
5. `_match_segments()` - 20 edges
6. `generate()` - 19 edges
7. `_segment_from_efforts()` - 17 edges
8. `_build_runs()` - 15 edges
9. `Strava-Analysis README` - 13 edges
10. `overview_sections()` - 12 edges

## Surprising Connections (you probably didn't know these)
- `_compute_threshold()` --calls--> `robust_threshold_mps()`  [INFERRED]
  generate_hub.py → lib/generate_analytics.py
- `_build_runs()` --calls--> `num()`  [INFERRED]
  generate_hub.py → lib/generate_analytics.py
- `_build_runs()` --calls--> `parse_date()`  [INFERRED]
  generate_hub.py → lib/generate_analytics.py
- `_build_runs()` --calls--> `fmt_pace()`  [INFERRED]
  generate_hub.py → lib/generate_dashboards.py
- `_build_runs()` --calls--> `is_interval()`  [INFERRED]
  generate_hub.py → lib/generate_dashboards.py

## Import Cycles
- 1-file cycle: `generate_hub.py -> generate_hub.py`
- 1-file cycle: `lib/generate_analytics.py -> lib/generate_analytics.py`

## Hyperedges (group relationships)
- **Compile-to-hub pipeline** — project_map_enriched_csv, compile_distance_stream, hub_build_runs, segdet_build_segments [EXTRACTED 1.00]
- **Segment cache invalidation triad** — caches_segments_cache, caches_runs_signature, project_map_params_token, caches_stale_trap [EXTRACTED 1.00]
- **Longitudinal fitness metrics** — analytics_session_loads, analytics_ctl_atl_tsb, analytics_acwr, analytics_monotony_strain [EXTRACTED 1.00]

## Communities (19 total, 1 thin omitted)

### Community 0 - "Analytics & Cache Concepts"
Cohesion: 0.05
Nodes (78): ACWR (acute:chronic workload ratio), analytics-and-goals skill, Critical speed model, CTL/ATL/TSB, Minetti grade adjustment (lib/grade.py), Hub threshold speed (robust fitted anchor), Monotony / strain (Foster), Riegel prediction (+70 more)

### Community 1 - "Analytics Computation Module"
Cohesion: 0.06
Nodes (59): _build_threshold_curve(), (date, best_5k_s) for every row carrying a 5K effort, sorted by date., acwr_series(), _best_5k_threshold_mps(), best_effort_points(), body_analytics(), cadence_trend(), calendar_data() (+51 more)

### Community 2 - "Best-Efforts & Goals Module"
Cohesion: 0.08
Nodes (45): body_best_efforts(), body_goal_dashboard(), derive_training_target(), fit_riegel(), fmt_date(), fmt_pace(), fmt_pace_bare(), fmt_pace_from_s_per_km() (+37 more)

### Community 3 - "Hub Generation & Classification"
Cohesion: 0.07
Nodes (43): _build_runs(), _classify_run(), _compute_pace_zones(), _compute_threshold(), _detect_pauses(), generate(), _haversine_km(), _heatmap_points() (+35 more)

### Community 4 - "Fit Parsing & Compile"
Cohesion: 0.10
Nodes (36): _best_efforts(), decompress_fit_gz(), _detect_reps(), _distance_stream(), extract_activity_id(), find_activities_csv(), find_strava_export(), _fit_keys_from() (+28 more)

### Community 5 - "Segment Naming & Road Snapping"
Cohesion: 0.10
Nodes (34): _bbox_span_ok(), _build_walk_graph(), _fallback_name(), _geo_key(), _graph_edges_latlon(), _index_edges(), _located_cache(), _located_names() (+26 more)

### Community 6 - "Corridor Mining & Cells"
Cohesion: 0.08
Nodes (26): _apply_stars(), _build_point_grid(), build_segments(), _cell_factory(), _cell_sequence(), _chain_length_m(), _disambiguate_names(), _extend_climb_chain() (+18 more)

### Community 7 - "Segment Dedupe & Anchoring"
Cohesion: 0.15
Nodes (17): _auto_anchor_segments(), _canonicalize_loops(), _dedupe(), _haversine_m(), _is_reverse_corridor(), _nearest_match(), _poly_centroid(), _polyline_deviation() (+9 more)

### Community 8 - "Effort Matching & Climbs"
Cohesion: 0.15
Nodes (14): _collect_efforts(), _loop_record(), _match_run(), _neighbours(), _net_gain(), Build an effort record (compatible with _segment_from_efforts) from a track span, 3x3 block of cells around c (~ one cell of slack on each side)., (net_elev, gain_m) for a sub-track, tolerant of points missing elevation. (+6 more)

### Community 9 - "Segment Classification & Elevation"
Cohesion: 0.15
Nodes (13): _absorb_efforts(), _classify(), _elev_profile(), _loop_instances(), _loop_roundness(), _median(), Elevation-over-distance samples [[metres_from_start, elev_m], ...] for a segment, Isoperimetric quotient 4*pi*Area / Perimeter**2 of a traversal's ground track (+5 more)

### Community 10 - "Anchored Loop Segments"
Cohesion: 0.17
Nodes (12): _anchor_eligible_runs(), _anchored_segment(), _build_anchored_segments(), _derive_anchor_line(), _match_anchor_spans(), The most typical run instance near the anchor's centre and length becomes its li, Even ~`step` m spacing along a list of {lat,lon} points -> [(lat,lon), ...]., All distance-bounded windows of one run's track that cover >= ANCHOR_COVER of th (+4 more)

### Community 11 - "Loop Candidate Detection"
Cohesion: 0.20
Nodes (11): _dominant_laps(), _group_loop_candidates(), _loop_centroid(), Closed laps inside one run's cell sequence, each at least `min_len`.      As t, Cluster genuine loop candidates by ground centroid so each physical loop locatio, Pick the dominant recurring lap at one location: the length band (within `ratio`, Nudge the loop's start/end indices within a small window to the pair of points, All self-crossing loop candidates across every run, each tagged with run_id + ce (+3 more)

### Community 12 - "Config Loading"
Cohesion: 0.36
Nodes (8): Config, load_config(), _parse_anchor(), _parse_race(), Path, Race, Load config.json if present. Never raises: a missing or malformed file     yield, SegmentAnchor

### Community 13 - "Loop Geometry & Medoid"
Cohesion: 0.25
Nodes (8): _best_roll(), _ll_to_xy(), _medoid_index(), Resample a closed loop to n points spaced equally by arc length., Cyclic offset of eff that best lines it up with ref (handles differing start poi, Index of the most representative effort: the one whose loop shape is closest to, _resample_closed(), _signed_area()

### Community 14 - "Loop Clustering"
Cohesion: 0.33
Nodes (6): _cluster_loops(), _debug_loop_cluster(), _detect_loops(), Group loop instances by geographic centroid and length. A running-mean centroid, Two passes. Phase 1 identifies distinct loops at a stable scale (a higher floor, SEG_DEBUG: report how an identified loop cluster's laps resolve into counted att

### Community 15 - "Reverse-Geocode Naming"
Cohesion: 0.33
Nodes (6): _derive_name(), _most_common(), _ordered_unique(), Reverse-geocode one point. `memo` (a per-build dict) collapses points that round, Query OSM for nearby road / feature names and compose a label. Returns     None, _reverse_geocode()

### Community 16 - "Loop Fragment Filtering"
Cohesion: 0.33
Nodes (6): _drop_loop_fragments(), _drop_through_loops(), _poly_coverage(), Fraction of `target` polyline points that lie within `tol` of any point on `by`., Drop any non-loop segment that traces most of a loop (>= LOOP_OVERLAP_FRAC of th, Drop a 'loop' that a straight point-to-point segment of comparable length traces

### Community 17 - "Geo-Track Densification"
Cohesion: 0.50
Nodes (4): _densify(), _geo_track(), Ordered list of dicts {lat, lon, d, t, elev} from a run's distance stream., Insert interpolated points so consecutive samples are <= DENSIFY_M apart.     K

## Knowledge Gaps
- **5 isolated node(s):** `Path`, `Path`, `Segment-detection debugging playbook`, `Chain (mined cell sequence)`, `VDOT trend (Daniels)`
  These have ≤1 connection - possible missing edges or undocumented components.
- **1 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `_segment_from_efforts()` connect `Segment Classification & Elevation` to `Analytics Computation Module`, `Best-Efforts & Goals Module`, `Segment Naming & Road Snapping`, `Corridor Mining & Cells`, `Segment Dedupe & Anchoring`, `Effort Matching & Climbs`, `Anchored Loop Segments`, `Loop Geometry & Medoid`, `Loop Clustering`?**
  _High betweenness centrality (0.133) - this node is a cross-community bridge._
- **Why does `main()` connect `Hub Generation & Classification` to `Analytics Computation Module`, `Best-Efforts & Goals Module`, `Corridor Mining & Cells`?**
  _High betweenness centrality (0.120) - this node is a cross-community bridge._
- **Why does `ga_time()` connect `Analytics Computation Module` to `Segment Classification & Elevation`, `Best-Efforts & Goals Module`, `Hub Generation & Classification`?**
  _High betweenness centrality (0.102) - this node is a cross-community bridge._
- **Are the 9 inferred relationships involving `project-map skill` (e.g. with `analytics-and-goals skill` and `caches-and-invalidation skill`) actually correct?**
  _`project-map skill` has 9 INFERRED edges - model-reasoned connections that need verification._
- **Are the 2 inferred relationships involving `generate_goal_dashboard()` (e.g. with `load_config()` and `ga_time()`) actually correct?**
  _`generate_goal_dashboard()` has 2 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Insert intermediate lat/lon points so no segment is longer than step_m metres.`, `Build heatmap points where each run contributes at most one point per ~100 m gri`, `Derive threshold speed (m/s) from a multi-point, grade-adjusted fitness curve` to the rest of the system?**
  _143 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Analytics & Cache Concepts` be split into smaller, more focused modules?**
  _Cohesion score 0.052947052947052944 - nodes in this community are weakly interconnected._