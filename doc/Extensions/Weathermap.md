# Network-WeatherMap with LibreNMS

Network Weathermap does not work on any supported PHP version. By
default, its pages have no access restriction and are open to everyone.

Do not use Network Weathermap. Use [Custom Maps](./Custom-Map.md)
instead.

## Migrating to Custom Maps

`scripts/weathermap_to_custom_map.py` converts Network Weathermap `.conf`
files into Custom Maps. You can convert one map, or a whole configs directory
at once. In directory mode, links between maps (via `INFOURL`) become links
between the new Custom Maps.

```bash
# Generate SQL to review before applying
./scripts/weathermap_to_custom_map.py /opt/librenms/html/plugins/Weathermap/configs \
    --sql-file all_maps.sql

# Or insert directly into the LibreNMS database
./scripts/weathermap_to_custom_map.py /opt/librenms/html/plugins/Weathermap/configs \
    --output direct
```

Run the script with `--help` to see all options, including how it handles
circular map links and devices or ports that no longer exist.
