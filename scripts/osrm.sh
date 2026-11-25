# Self-hosted OSRM for drive, bike and walk legs (design.md, commute). Run from the repo root via the flake:
#   nix run .#osrm -- build   download the Geofabrik NorCal extract, clip it to the Bay Area box, build car,
#                             bicycle and foot graphs (MLD) under data/osrm/
#   nix run .#osrm -- serve   run three osrm-routed on 127.0.0.1 (car 5000, bicycle 5001, foot 5002)
# OpenStreetMap data is non-listing reference data (AGENTS.md rule 6). ODbL: (c) OpenStreetMap contributors.
set -euo pipefail
dir="${APTF_OSRM_DIR:-data/osrm}"
# lon/lat box around every place we store (Santa Cruz to Sonoma, the coast to east of San Jose)
bbox="${APTF_OSRM_BBOX:--123.1,36.8,-121.3,38.7}"
url="https://download.geofabrik.de/north-america/us/california/norcal-latest.osm.pbf"
profiles="${OSRM_PROFILES:?OSRM_PROFILES is set by the flake}"
declare -A port=([car]=5000 [bicycle]=5001 [foot]=5002)

case "${1:-}" in
build)
  mkdir -p "$dir"
  if [ ! -f "$dir/norcal.osm.pbf" ] || [ "${2:-}" = "--refresh" ]; then
    echo "downloading $url"
    curl -fL --retry 3 -o "$dir/norcal.osm.pbf.part" "$url"
    mv "$dir/norcal.osm.pbf.part" "$dir/norcal.osm.pbf"
  fi
  echo "clipping to $bbox"
  osmium extract -b "$bbox" --overwrite -o "$dir/bayarea.osm.pbf" "$dir/norcal.osm.pbf"
  for p in car bicycle foot; do
    echo "building $p"
    mkdir -p "$dir/$p"
    cp "$dir/bayarea.osm.pbf" "$dir/$p/bayarea.osm.pbf"
    osrm-extract -p "$profiles/$p.lua" "$dir/$p/bayarea.osm.pbf"
    osrm-partition "$dir/$p/bayarea.osrm"
    osrm-customize "$dir/$p/bayarea.osrm"
    rm "$dir/$p/bayarea.osm.pbf"
  done
  date -u +%FT%TZ > "$dir/built_at"
  echo "built in $dir"
  ;;
serve)
  pids=()
  for p in car bicycle foot; do
    [ -f "$dir/$p/bayarea.osrm.mldgr" ] || { echo "no $p graph in $dir: run 'nix run .#osrm -- build' first" >&2; exit 1; }
    osrm-routed --algorithm mld --ip 127.0.0.1 --port "${port[$p]}" "$dir/$p/bayarea.osrm" &
    pids+=($!)
  done
  trap 'kill "${pids[@]}" 2>/dev/null' EXIT
  wait -n
  exit 1   # one server stopped: let systemd restart all three
  ;;
*)
  echo "usage: osrm.sh build [--refresh] | serve" >&2; exit 2 ;;
esac
