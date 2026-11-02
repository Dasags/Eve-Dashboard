import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.services.map_data import _download


class MapDataTests(unittest.TestCase):
    def test_map_data_contains_region_ids_and_security(self):
        tables = {
            "mapSolarSystems.csv": [
                {
                    "solarSystemID": "30000001",
                    "solarSystemName": "System One",
                    "regionID": "10000001",
                    "x": "10",
                    "z": "20",
                    "security": "0.9",
                },
                {
                    "solarSystemID": "30000002",
                    "solarSystemName": "System Two",
                    "regionID": "10000001",
                    "x": "12",
                    "z": "22",
                    "security": "0.4",
                },
            ],
            "mapSolarSystemJumps.csv": [
                {"fromSolarSystemID": "30000001", "toSolarSystemID": "30000002"}
            ],
            "mapRegions.csv": [{"regionID": "10000001", "regionName": "Test Region"}],
        }

        async def load_table(_client, filename):
            return tables[filename]

        with patch("app.services.map_data._csv", new=AsyncMock(side_effect=load_table)):
            result = asyncio.run(_download())

        self.assertEqual(len(result["systems"]), 2)
        self.assertEqual(result["systems"][0]["security"], 0.9)
        self.assertEqual(result["systems"][1]["x"], 12.0)
        self.assertEqual(result["systems"][0]["y"], 20.0)
        self.assertEqual(result["region_centers"][0]["region_id"], 10000001)
        self.assertEqual(result["region_centers"][0]["x"], 11.0)
        self.assertEqual(result["region_centers"][0]["y"], 21.0)
        self.assertEqual(result["connections"], [[30000001, 30000002]])


if __name__ == "__main__":
    unittest.main()
