import json
import xml.etree.cElementTree as et
from collections import ChainMap
from typing import Any, Tuple

from dodal.devices.oav.oav_errors import (
    OAVError_BeamPositionNotFound,
    OAVError_ZoomLevelNotFound,
)
from dodal.log import LOGGER

OAV_CONFIG_FILE_DEFAULTS = {
    "zoom_params_file": "/dls_sw/i03/software/gda/configurations/i03-config/xml/jCameraManZoomLevels.xml",
    "oav_config_json": "/dls_sw/i03/software/gda/configurations/i03-config/etc/OAVCentring.json",
    "display_config": "/dls_sw/i03/software/gda_versions/var/display.configuration",
}


class OAVParameters:
    def __init__(
        self,
        context="loopCentring",
        zoom_params_file=OAV_CONFIG_FILE_DEFAULTS["zoom_params_file"],
        oav_config_json=OAV_CONFIG_FILE_DEFAULTS["oav_config_json"],
        display_config=OAV_CONFIG_FILE_DEFAULTS["display_config"],
    ):
        self.zoom_params_file: str = zoom_params_file
        self.oav_config_json: str = oav_config_json
        self.display_config: str = display_config
        self.context = context

        self.global_params, self.context_dicts = self.load_json(self.oav_config_json)
        self.active_params: ChainMap = ChainMap(
            self.context_dicts[self.context], self.global_params
        )
        self.update_self_from_current_context()
        self.load_microns_per_pixel()
        self._extract_beam_position()

    @staticmethod
    def load_json(filename: str) -> tuple[dict[str, Any], dict[str, dict]]:
        """
        Loads the json from the specified file, and returns a dict with all the
        individual top-level k-v pairs, and one with all the subdicts.
        """
        with open(filename) as f:
            raw_params: dict[str, Any] = json.load(f)
        global_params = {
            k: raw_params.pop(k)
            for k, v in list(raw_params.items())
            if not isinstance(v, dict)
        }
        context_dicts = raw_params
        return global_params, context_dicts

    def update_context(self, context: str) -> None:
        self.active_params.maps.pop()
        self.active_params = self.active_params.new_child(self.context_dicts[context])

    def update_self_from_current_context(self) -> None:
        def update(name, param_type, default=None):
            param = self.active_params.get(name, default)
            try:
                param = param_type(param)
                return param
            except AssertionError:
                raise TypeError(
                    f"OAV param {name} from the OAV centring params json file has the "
                    f"wrong type, should be {param_type} but is {type(param)}."
                )

        self.exposure: float = update("exposure", float)
        self.acquire_period: float = update("acqPeriod", float)
        self.gain: float = update("gain", float)
        self.canny_edge_upper_threshold: float = update(
            "CannyEdgeUpperThreshold", float
        )
        self.canny_edge_lower_threshold: float = update(
            "CannyEdgeLowerThreshold", float, default=5.0
        )
        self.minimum_height: int = update("minheight", int)
        self.zoom: float = update("zoom", float)
        self.preprocess: int = update(
            "preprocess", int
        )  # gets blur type, e.g. 8 = gaussianBlur, 9 = medianBlur
        self.preprocess_K_size: int = update(
            "preProcessKSize", int
        )  # length scale for blur preprocessing
        self.detection_script_filename: str = update("filename", str)
        self.close_ksize: int = update("close_ksize", int, default=11)
        self.min_callback_time: float = update("min_callback_time", float, default=0.08)
        self.direction: int = update("direction", int)
        self.max_tip_distance: float = update("max_tip_distance", float, default=300)

    def load_microns_per_pixel(self, zoom=None):
        """
        Loads the microns per x pixel and y pixel for a given zoom level. These are
        currently generated by GDA, though artemis could generate them in future.
        """
        if not zoom:
            zoom = self.zoom

        tree = et.parse(self.zoom_params_file)
        self.micronsPerXPixel = self.micronsPerYPixel = None
        root = tree.getroot()
        levels = root.findall(".//zoomLevel")
        for node in levels:
            if float(node.find("level").text) == zoom:
                self.micronsPerXPixel = float(node.find("micronsPerXPixel").text)
                self.micronsPerYPixel = float(node.find("micronsPerYPixel").text)
        if self.micronsPerXPixel is None or self.micronsPerYPixel is None:
            raise OAVError_ZoomLevelNotFound(
                f"Could not find the micronsPer[X,Y]Pixel parameters in {self.zoom_params_file} for zoom level {zoom}."
            )

        # get the max tip distance in pixels
        self.max_tip_distance_pixels = self.max_tip_distance / self.micronsPerXPixel

    def _extract_beam_position(self):
        """
        Extracts the beam location in pixels `xCentre` `yCentre`. The beam location is
        stored in the file display.configuration. The beam location is manually inputted
        by the beamline operator GDA by clicking where on screen a scintillator ligths up.
        """
        with open(self.display_config, "r") as f:
            file_lines = f.readlines()
            for i in range(len(file_lines)):
                if file_lines[i].startswith("zoomLevel = " + str(self.zoom)):
                    crosshair_x_line = file_lines[i + 1]
                    crosshair_y_line = file_lines[i + 2]
                    break

            if crosshair_x_line is None or crosshair_y_line is None:
                raise OAVError_BeamPositionNotFound(
                    f"Could not extract beam position at zoom level {self.zoom}"
                )

            self.beam_centre_i = int(crosshair_x_line.split(" = ")[1])
            self.beam_centre_j = int(crosshair_y_line.split(" = ")[1])

            self.beam_centre_x = int(crosshair_x_line.split(" = ")[1])
            self.beam_centre_y = int(crosshair_y_line.split(" = ")[1])
            LOGGER.info(f"Beam centre: {self.beam_centre_i, self.beam_centre_j}")

    def calculate_beam_distance(
        self, horizontal_pixels: int, vertical_pixels: int
    ) -> Tuple[int, int]:
        """
        Calculates the distance between the beam centre and the given (horizontal, vertical).

        Args:
            horizontal_pixels (int): The x (camera coordinates) value in pixels.
            vertical_pixels (int): The y (camera coordinates) value in pixels.
        Returns:
            The distance between the beam centre and the (horizontal, vertical) point in pixels as a tuple
            (horizontal_distance, vertical_distance).
        """

        return (
            self.beam_centre_i - horizontal_pixels,
            self.beam_centre_j - vertical_pixels,
        )
