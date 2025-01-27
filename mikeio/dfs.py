from datetime import datetime, timedelta
from abc import abstractmethod
from typing import Optional

import warnings
import numpy as np
import pandas as pd
from tqdm import tqdm, trange

from mikeio.spatial.geometry import GeometryUndefined
from .dataset import Dataset
from .base import TimeSeries

from .dfsutil import _valid_item_numbers, _valid_timesteps, _get_item_info
from .eum import ItemInfo, TimeStepUnit, EUMType, EUMUnit
from .custom_exceptions import DataDimensionMismatch, ItemNumbersError
from mikecore.eum import eumQuantity
from mikecore.DfsFile import DfsSimpleType, TimeAxisType, DfsFile
from mikecore.DfsFactory import DfsFactory

def _write_dfs_data(*, dfs: DfsFile, ds: Dataset, n_spatial_dims: int) -> None:

    deletevalue = dfs.FileInfo.DeleteValueFloat  # ds.deletevalue
    has_no_time = "time" not in ds.dims
    if ds.is_equidistant:
        t_rel = np.zeros(ds.n_timesteps)
    else:
        t_rel = (ds.time - ds.time[0]).total_seconds()
    
    for i in range(ds.n_timesteps):
        for item in range(ds.n_items):

            if has_no_time:
                d = ds[item].values
            else:
                d = ds[item].values[i]
            d = d.copy()  # to avoid modifying the input
            d[np.isnan(d)] = deletevalue

            d = d.reshape(ds.shape[-n_spatial_dims:])  # spatial axes
            darray = d.flatten()

            dfs.WriteItemTimeStepNext(t_rel[i], darray.astype(np.float32))

    dfs.Close()


class _Dfs123(TimeSeries):

    show_progress = False

    def __init__(self, filename=None):
        self._filename = str(filename)
        self._projstr = None
        self._start_time = None
        self._end_time = None
        self._is_equidistant = True
        self._items = None
        self._builder = None
        self._factory = None
        self._deletevalue = None
        self._override_coordinates = False
        self._timeseries_unit = TimeStepUnit.SECOND
        self._dt = None
        self.geometry = GeometryUndefined()
        self._dfs = None
        self._source = None

    def read(
        self,
        *,
        items=None,
        time=None,
        keepdims=False,
        dtype=np.float32,
    ) -> Dataset:
        """
        Read data from a dfs file

        Parameters
        ---------
        items: list[int] or list[str], optional
            Read only selected items, by number (0-based), or by name
        time: int, str, datetime, pd.TimeStamp, sequence, slice or pd.DatetimeIndex, optional
            Read only selected time steps, by default None (=all)
        keepdims: bool, optional
            When reading a single time step only, should the time-dimension be kept
            in the returned Dataset? by default: False

        Returns
        -------
        Dataset
        """

        self._open()

        item_numbers = _valid_item_numbers(self._dfs.ItemInfo, items)
        n_items = len(item_numbers)

        single_time_selected, time_steps = _valid_timesteps(self._dfs.FileInfo, time)
        nt = len(time_steps) if not single_time_selected else 1

        if self._ndim == 1:
            shape = (nt, self._nx)
        elif self._ndim == 2:
            shape = (nt, self._ny, self._nx)
        else:
            shape = (nt, self._nz, self._ny, self._nx)

        if single_time_selected and not keepdims:
            shape = shape[1:]

        data_list = [np.ndarray(shape=shape, dtype=dtype) for item in range(n_items)]

        t_seconds = np.zeros(len(time_steps))

        for i, it in enumerate(tqdm(time_steps, disable=not self.show_progress)):
            for item in range(n_items):

                itemdata = self._dfs.ReadItemTimeStep(item_numbers[item] + 1, int(it))

                src = itemdata.Data
                d = src

                d[d == self.deletevalue] = np.nan

                if self._ndim == 2:
                    d = d.reshape(self._ny, self._nx)

                if single_time_selected:
                    data_list[item] = d
                else:
                    data_list[item][i] = d

            t_seconds[i] = itemdata.Time

        time = pd.to_datetime(t_seconds, unit="s", origin=self.start_time)

        items = _get_item_info(self._dfs.ItemInfo, item_numbers)

        self._dfs.Close()
        return Dataset(data_list, time, items, geometry=self.geometry, validate=False)

    def _read_header(self):
        dfs = self._dfs
        self._n_items = len(dfs.ItemInfo)
        self._items = self._get_item_info(list(range(self._n_items)))
        self._timeaxistype = dfs.FileInfo.TimeAxis.TimeAxisType
        if self._timeaxistype in {
            TimeAxisType.CalendarEquidistant,
            TimeAxisType.CalendarNonEquidistant,
        }:
            self._start_time = dfs.FileInfo.TimeAxis.StartDateTime
        else:  # relative time axis
            self._start_time = datetime(1970, 1, 1)
        if hasattr(dfs.FileInfo.TimeAxis, "TimeStep"):
            self._timestep_in_seconds = (
                dfs.FileInfo.TimeAxis.TimeStep
            )  # TODO handle other timeunits
            # TODO to get the EndTime
        self._n_timesteps = dfs.FileInfo.TimeAxis.NumberOfTimeSteps
        projstr = dfs.FileInfo.Projection.WKTString
        self._projstr = "NON-UTM" if not projstr else projstr
        self._longitude = dfs.FileInfo.Projection.Longitude
        self._latitude = dfs.FileInfo.Projection.Latitude
        self._orientation = dfs.FileInfo.Projection.Orientation
        self._deletevalue = dfs.FileInfo.DeleteValueFloat

        dfs.Close()

    def _write(
        self,
        *,
        filename,
        data,
        dt,
        coordinate=None,
        title,
        keep_open=False,
    ):

        neq_datetimes = None
        if isinstance(data, Dataset) and not data.is_equidistant:
            neq_datetimes = data.time

        self._write_handle_common_arguments(
            title=title, data=data, coordinate=coordinate, dt=dt
        )

        shape = np.shape(data[0])
        t_offset = 0 if len(shape) == self._ndim else 1
        if self._ndim == 1:
            self._nx = shape[t_offset + 0]
        elif self._ndim == 2:
            self._ny = shape[t_offset + 0]
            self._nx = shape[t_offset + 1]
        elif self._ndim == 3:
            self._nz = shape[t_offset + 0]
            self._ny = shape[t_offset + 1]
            self._nx = shape[t_offset + 2]

        self._factory = DfsFactory()
        self._set_spatial_axis()

        if self._ndim == 1:
            if not all(np.shape(d)[t_offset + 0] == self._nx for d in self._data):
                raise DataDimensionMismatch()

        if self._ndim == 2:
            if not all(np.shape(d)[t_offset + 0] == self._ny for d in self._data):
                raise DataDimensionMismatch()

            if not all(np.shape(d)[t_offset + 1] == self._nx for d in self._data):
                raise DataDimensionMismatch()

        if neq_datetimes is not None:
            self._is_equidistant = False
            start_time = neq_datetimes[0]
            self._start_time = start_time

        dfs = self._setup_header(filename)
        self._dfs = dfs

        deletevalue = dfs.FileInfo.DeleteValueFloat  # -1.0000000031710769e-30

        for i in trange(self._n_timesteps, disable=not self.show_progress):
            for item in range(self._n_items):

                d = self._data[item][i] if t_offset == 1 else self._data[item]
                d = d.copy()  # to avoid modifying the input
                d[np.isnan(d)] = deletevalue

                if self._is_equidistant:
                    dfs.WriteItemTimeStepNext(0, d.astype(np.float32))
                else:
                    t = neq_datetimes[i]
                    relt = (t - self._start_time).total_seconds()
                    dfs.WriteItemTimeStepNext(relt, d.astype(np.float32))

        if not keep_open:
            dfs.Close()
        else:
            return self

    def append(self, data: Dataset) -> None:
        """Append to a dfs file opened with `write(...,keep_open=True)`

        Parameters
        -----------
        data: Dataset
        """

        deletevalue = self._dfs.FileInfo.DeleteValueFloat  # -1.0000000031710769e-30

        for i in trange(self._n_timesteps, disable=not self.show_progress):
            for item in range(self._n_items):

                d = data[item].to_numpy()[i]
                d = d.copy()  # to avoid modifying the input
                d[np.isnan(d)] = deletevalue

                if self._ndim == 1:
                    darray = d

                if self._ndim == 2:
                    d = d.reshape(self.shape[1:])
                    darray = d.reshape(d.size, 1)[:, 0]

                if self._is_equidistant:
                    self._dfs.WriteItemTimeStepNext(0, darray.astype(np.float32))
                else:
                    raise NotImplementedError(
                        "Append is not yet available for non-equidistant files"
                    )

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self._dfs.Close()

    def close(self):
        "Finalize write for a dfs file opened with `write(...,keep_open=True)`"
        self._dfs.Close()

    def _write_handle_common_arguments(self, *, title, data, coordinate, dt):

        if title is None:
            self._title = ""

        self._n_timesteps = np.shape(data[0])[0]
        self._n_items = len(data)

        if coordinate is None:
            if self._projstr is not None:
                self._coordinate = [
                    self._projstr,
                    self._longitude,
                    self._latitude,
                    self._orientation,
                ]
            elif isinstance(data, Dataset) and (data.geometry is not None):
                self._coordinate = [
                    data.geometry.projection_string,
                    data.geometry.origin[0],
                    data.geometry.origin[1],
                    data.geometry.orientation,
                ]
            else:
                warnings.warn("No coordinate system provided")
                self._coordinate = ["LONG/LAT", 0, 0, 0]
        else:
            self._override_coordinates = True
            self._coordinate = coordinate

        if isinstance(data, Dataset):
            self._items = data.items
            self._start_time = data.time[0]
            self._n_timesteps = len(data.time)
            if dt is None and len(data.time) > 1:
                self._dt = (data.time[1] - data.time[0]).total_seconds()
            self._data = data.to_numpy()
        else:
            raise TypeError("data must be supplied in the form of a mikeio.Dataset")

        if dt:
            self._dt = dt

        if self._dt is None:
            self._dt = 1
            if self._n_timesteps > 1:
                warnings.warn("No timestep supplied. Using 1s.")

        if self._items is None:
            self._items = [ItemInfo(f"Item {i+1}") for i in range(self._n_items)]

        self._timeseries_unit = TimeStepUnit.SECOND

    def _setup_header(self, filename):

        system_start_time = self._start_time

        self._builder.SetDataType(0)

        proj = self._factory.CreateProjectionGeoOrigin(*self._coordinate)

        self._builder.SetGeographicalProjection(proj)

        if self._is_equidistant:
            self._builder.SetTemporalAxis(
                self._factory.CreateTemporalEqCalendarAxis(
                    self._timeseries_unit, system_start_time, 0, self._dt
                )
            )
        else:
            self._builder.SetTemporalAxis(
                self._factory.CreateTemporalNonEqCalendarAxis(
                    self._timeseries_unit, system_start_time
                )
            )

        for item in self._items:
            self._builder.AddCreateDynamicItem(
                item.name,
                eumQuantity.Create(item.type, item.unit),
                DfsSimpleType.Float,
                item.data_value_type,
            )

        try:
            self._builder.CreateFile(filename)
        except IOError:
            # TODO does this make sense?
            print("cannot create dfs file: ", filename)

        return self._builder.GetFile()

    def _open(self):
        raise NotImplementedError("Should be implemented by subclass")

    def _set_spatial_axis(self):
        raise NotImplementedError("Should be implemented by subclass")

    def _find_item(self, item_names):
        """Utility function to find item numbers

        Parameters
        ----------
        dfs : DfsFile

        item_names : list[str]
            Names of items to be found

        Returns
        -------
        list[int]
            item numbers (0-based)

        Raises
        ------
        KeyError
            In case item is not found in the dfs file
        """
        names = [x.Name for x in self._dfs.ItemInfo]
        item_lookup = {name: i for i, name in enumerate(names)}
        try:
            item_numbers = [item_lookup[x] for x in item_names]
        except KeyError:
            raise KeyError(f"Selected item name not found. Valid names are {names}")

        return item_numbers

    def _get_item_info(self, item_numbers):
        """Read DFS ItemInfo

        Parameters
        ----------
        dfs : MIKE dfs object
        item_numbers : list[int]

        Returns
        -------
        list[Iteminfo]
        """
        items = []
        for item in item_numbers:
            name = self._dfs.ItemInfo[item].Name
            eumItem = self._dfs.ItemInfo[item].Quantity.Item
            eumUnit = self._dfs.ItemInfo[item].Quantity.Unit
            itemtype = EUMType(eumItem)
            unit = EUMUnit(eumUnit)
            data_value_type = self._dfs.ItemInfo[item].ValueType
            item = ItemInfo(name, itemtype, unit, data_value_type)
            items.append(item)
        return items

    def _validate_item_numbers(self, item_numbers):
        if not all(
            isinstance(item_number, int) and 0 <= item_number < self.n_items
            for item_number in item_numbers
        ):
            raise ItemNumbersError()

    @property
    def deletevalue(self):
        "File delete value"
        return self._deletevalue

    @property
    def n_items(self):
        "Number of items"
        return self._n_items

    @property
    def items(self):
        "List of items"
        return self._items

    @property
    def start_time(self):
        """File start time"""
        return self._start_time

    @property
    def end_time(self):
        """File end time"""
        if self._end_time is None:
            self._end_time = self.read(items=[0]).time[-1].to_pydatetime()

        return self._end_time

    @property
    def n_timesteps(self) -> int:
        """Number of time steps"""
        return self._n_timesteps

    @property
    def timestep(self) -> Optional[float]:
        """Time step size in seconds"""
        if self._timeaxistype in {
            TimeAxisType.CalendarEquidistant,
            TimeAxisType.TimeEquidistant,
        }:
            return self._dfs.FileInfo.TimeAxis.TimeStepInSeconds()

    @property
    def time(self) -> Optional[pd.DatetimeIndex]:
        """File all datetimes"""
        if self._timeaxistype in {
            TimeAxisType.CalendarEquidistant,
            TimeAxisType.TimeEquidistant,
        }:
            return pd.to_datetime(
                [
                    self.start_time + timedelta(seconds=i * self.timestep)
                    for i in range(self.n_timesteps)
                ]
            )

        else:
            return None

    @property
    def projection_string(self):
        return self._projstr

    @property
    def longitude(self):
        """Origin longitude"""
        return self._longitude

    @property
    def latitude(self):
        """Origin latitude"""
        return self._latitude

    @property
    def origin(self):
        """Origin (in own CRS)"""
        return self.geometry.origin

    @property
    def orientation(self):
        """Orientation (in own CRS)"""
        return self.geometry.orientation

    @property
    @abstractmethod
    def dx(self):
        """Step size in x direction"""
        pass
