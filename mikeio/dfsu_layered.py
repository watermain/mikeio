from mikecore.DfsuFile import DfsuFileType
import numpy as np
from functools import wraps
from scipy.spatial import cKDTree

from .dfsu import _Dfsu
from .dataset import Dataset, DataArray
from .spatial.FM_geometry import GeometryFMLayered
from .custom_exceptions import InvalidGeometry
from .dfsutil import _valid_timesteps
from .interpolation import get_idw_interpolant, interp2d
from .eum import ItemInfo, EUMType


class DfsuLayered(_Dfsu):
    @wraps(GeometryFMLayered.to_2d_geometry)
    def to_2d_geometry(self):
        return self.geometry2d

    @property
    def geometry2d(self):
        """The 2d geometry for a 3d object"""
        return self._geometry2d

    @property
    def n_layers(self):
        """Maximum number of layers"""
        return self.geometry._n_layers

    @property
    def n_sigma_layers(self):
        """Number of sigma layers"""
        return self.geometry.n_sigma_layers

    @property
    def n_z_layers(self):
        """Maximum number of z-layers"""
        if self.n_layers is None:
            return None
        return self.n_layers - self.n_sigma_layers

    @property
    def e2_e3_table(self):
        """The 2d-to-3d element connectivity table for a 3d object"""
        if self.n_layers is None:
            print("Object has no layers: cannot return e2_e3_table")
            return None
        return self.geometry.e2_e3_table

    @property
    def elem2d_ids(self):
        """The associated 2d element id for each 3d element"""
        if self.n_layers is None:
            raise InvalidGeometry("Object has no layers: cannot return elem2d_ids")
        return self.geometry.elem2d_ids

    @property
    def layer_ids(self):
        """The layer number (0=bottom, 1, 2, ...) for each 3d element"""
        if self.n_layers is None:
            raise InvalidGeometry("Object has no layers: cannot return layer_ids")
        return self.geometry.layer_ids

    @property
    def top_elements(self):
        """List of 3d element ids of surface layer"""
        if self.n_layers is None:
            print("Object has no layers: cannot find top_elements")
            return None
        return self.geometry.top_elements

    @property
    def n_layers_per_column(self):
        """List of number of layers for each column"""
        if self.n_layers is None:
            print("Object has no layers: cannot find n_layers_per_column")
            return None
        return self.geometry.n_layers_per_column

    @property
    def bottom_elements(self):
        """List of 3d element ids of bottom layer"""
        if self.n_layers is None:
            print("Object has no layers: cannot find bottom_elements")
            return None
        return self.geometry.bottom_elements

    @wraps(GeometryFMLayered.get_layer_elements)
    def get_layer_elements(self, layer):
        if self.n_layers is None:
            raise InvalidGeometry("Object has no layers: cannot get_layer_elements")
        return self.geometry.get_layer_elements(layer)


class Dfsu2DV(DfsuLayered):
    def plot_vertical_profile(
        self, values, time_step=None, cmin=None, cmax=None, label="", **kwargs
    ):
        """
        Plot unstructured vertical profile

        Parameters
        ----------
        values: np.array
            value for each element to plot
        timestep: int, optional
            the timestep that fits with the data to get correct vertical
            positions, default: use static vertical positions
        cmin: real, optional
            lower bound of values to be shown on plot, default:None
        cmax: real, optional
            upper bound of values to be shown on plot, default:None
        title: str, optional
            axes title
        label: str, optional
            colorbar label
        cmap: matplotlib.cm.cmap, optional
            colormap, default viridis
        figsize: (float, float), optional
            specify size of figure
        ax: matplotlib.axes, optional
            Adding to existing axis, instead of creating new fig

        Returns
        -------
        <matplotlib.axes>
        """
        import matplotlib.pyplot as plt
        from matplotlib.collections import PolyCollection

        if isinstance(values, DataArray):
            values = values.to_numpy()

        nc = self.node_coordinates
        x_coordinate = np.hypot(nc[:, 0], nc[:, 1])
        if time_step is None:
            y_coordinate = nc[:, 2]
        else:
            raise NotImplementedError()  # TODO
            # y_coordinate = self.read()[0].to_numpy()[time_step, :]

        elements = self._Get_2DVertical_elements()

        # plot in existing or new axes?
        if "ax" in kwargs:
            ax = kwargs["ax"]
        else:
            figsize = None
            if "figsize" in kwargs:
                figsize = kwargs["figsize"]
            _, ax = plt.subplots(figsize=figsize)

        yz = np.c_[x_coordinate, y_coordinate]
        verts = yz[elements]

        if "cmap" in kwargs:
            cmap = kwargs["cmap"]
        else:
            cmap = "jet"
        pc = PolyCollection(verts, cmap=cmap)

        if cmin is None:
            cmin = np.nanmin(values)
        if cmax is None:
            cmax = np.nanmax(values)
        pc.set_clim(cmin, cmax)

        plt.colorbar(pc, ax=ax, label=label, orientation="vertical")
        pc.set_array(values)

        if "edge_color" in kwargs:
            edge_color = kwargs["edge_color"]
        else:
            edge_color = None
        pc.set_edgecolor(edge_color)

        ax.add_collection(pc)
        ax.autoscale()

        if "title" in kwargs:
            ax.set_title(kwargs["title"])

        return ax

    def _Get_2DVertical_elements(self):
        if (self._type == DfsuFileType.DfsuVerticalProfileSigmaZ) or (
            self._type == DfsuFileType.DfsuVerticalProfileSigma
        ):
            elements = [
                list(self.geometry.element_table[i])
                for i in range(len(list(self.geometry.element_table)))
            ]
            return np.asarray(elements)  # - 1


class Dfsu3D(DfsuLayered):
    def find_nearest_profile_elements(self, x, y):
        """Find 3d elements of profile nearest to (x,y) coordinates

        Parameters
        ----------
        x : float
            x-coordinate of point
        y : float
            y-coordinate of point

        Returns
        -------
        np.array(int)
            element ids of vertical profile
        """
        if self.is_2d:
            raise InvalidGeometry("Object is 2d. Cannot get_nearest_profile")
        else:
            elem2d, _ = self.geometry._find_n_nearest_2d_elements(x, y)
            elem3d = self.geometry.e2_e3_table[elem2d]
            return elem3d

    def extract_surface_elevation_from_3d(self, filename=None, time=None, n_nearest=4):
        """
        Extract surface elevation from a 3d dfsu file (based on zn)
        to a new 2d dfsu file with a surface elevation item.

        Parameters
        ---------
        filename: str
            Output file name
        time: str, int or list[int], optional
            Extract only selected time_steps
        n_nearest: int, optional
            number of points for spatial interpolation (inverse_distance), default=4

        Examples
        --------
        >>> dfsu.extract_surface_elevation_from_3d('ex_surf.dfsu', time='2018-1-1,2018-2-1')
        """
        # validate input
        assert (
            self._type == DfsuFileType.Dfsu3DSigma
            or self._type == DfsuFileType.Dfsu3DSigmaZ
        )
        assert n_nearest > 0
        time_steps = _valid_timesteps(self._source, time)

        # make 2d nodes-to-elements interpolator
        top_el = self.top_elements
        geom = self.geometry.elements_to_geometry(top_el, node_layers="top")
        xye = geom.element_coordinates[:, 0:2]
        xyn = geom.node_coordinates[:, 0:2]
        tree2d = cKDTree(xyn)
        dist, node_ids = tree2d.query(xye, k=n_nearest)
        if n_nearest == 1:
            weights = None
        else:
            weights = get_idw_interpolant(dist)

        # read zn from 3d file and interpolate to element centers
        ds = self.read(items=0, time_steps=time_steps)  # read only zn
        node_ids_surf, _ = self.geometry._get_nodes_and_table_for_elements(
            top_el, node_layers="top"
        )
        zn_surf = ds.data[0][:, node_ids_surf]  # surface
        surf2d = interp2d(zn_surf, node_ids, weights)

        # create output
        items = [ItemInfo(EUMType.Surface_Elevation)]
        ds2 = Dataset([surf2d], ds.time, items, geometry=geom)
        if filename is None:
            return ds2
        else:
            title = "Surface extracted from 3D file"
            self.write(filename, ds2, elements=top_el, title=title)