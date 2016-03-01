# Licensed under the GPLv3 - see LICENSE.rst
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)
import io
import numpy as np
import astropy.units as u
from ..vlbi_base.base import (VLBIStreamBase,
                              VLBIStreamReaderBase, VLBIStreamWriterBase)
from .header import GSBHeader
from .payload import GSBPayload
from .frame import GSBFrame

__all__ = ['GSBFileReader', 'GSBFileWriter', 'GSBStreamReader',
           'GSBStreamWriter', 'open']


class GSBTimeStampIO(io.TextIOWrapper):
    """Simple reader/writer for GSB time stamp files.

    Adds ``read_timestamp`` and ``write_timestamp`` methods to the basic text
    file wrapper :class:`~io.TextIOWrapper`.
    """
    def read_timestamp(self):
        """Read a single timestamp.

        Returns
        -------
        frame : `~baseband.gsb.GSBHeader`
            With a ``.time`` property that returns the time encoded.
        """
        return GSBHeader.fromfile(self)

    def write_timestamp(self, header=None, **kwargs):
        """Write a single timestamp.

        Parameters
        ----------
        header : `~baseband.gsb.GSBHeader`, optional
            Header holding time to be written to disk.
        **kwargs :
            If no header is given, these are used to initialize one.
        """
        if header is None:
            header = GSBHeader(**kwargs)
        header.tofile(self)


class GSBFileReader(io.BufferedReader):
    """Simple reader for GSB data files.

    Adds ``read_payload`` method to the basic binary file reader
    :class:`~io.BufferedReader`.
    """
    def read_payload(self, payloadsize, nchan=1, bps=4, complex_data=False):
        """Read a single block.

        Parameters
        ----------
        payloadsize : int
            Number of bytes to read.
        nchan : int
            Number of channels in the data.  Default: 1.
        bps : int
            Number of bits per sample (or real/imaginary component).
            Default: 4.
        complex_data : bool
            Whether data is complex or float.  Default: False.

        Returns
        -------
        frame : `~baseband.gsb.GSBPayload`
            With a ``.data`` property that returns the data encoded.
        """
        return GSBPayload.fromfile(self, payloadsize=payloadsize,
                                   nchan=nchan, bps=bps,
                                   complex_data=complex_data)


class GSBFileWriter(io.BufferedWriter):
    """Simple writer for GSB data files.

    Adds ``write_payload`` method to the basic binary file writer
    :class:`~io.BufferedWriter`.
    """
    def write_payload(self, data, bps=4):
        """Write single data block.

        Parameters
        ----------
        data : array or :`~baseband.gsb.GSBPayload`
            If an array, ``bps`` needs to be passed in.
        bps : int, optional
            The number of bits per sample to be used to encode the payload.
            Ignored if `data` is a GSB payload.  Default: 4.
        """
        if not isinstance(data, GSBPayload):
            data = GSBPayload.fromdata(data, bps=bps)

        return data.tofile(self)


class GSBStreamBase(VLBIStreamBase):

    def close(self):
        for fh_pair in self.fh_raw:
            for fh in fh_pair:
                fh.close()


class GSBStreamReader(VLBIStreamReaderBase, GSBStreamBase):
    def __init__(self, fh_ts, fh_raw, thread_ids=None,
                 nchan=1, bps=4, complex_data=None,
                 samples_per_frame=1 << 25,
                 frames_per_second=None):
        self.fh_ts = fh_ts
        header0 = fh_ts.read_timestamp()
        self._headersize = fh_ts.tell()
        if complex_data is None:
            complex_data = False if header0.mode == 'rawdump' else True
        self._complex_data = complex_data
        if frames_per_second is None:
            header1 = fh_ts.read_timestamp()
            assert fh_ts.tell() == 2 * self._headersize
            frames_per_second = (1./(header1.time -
                                     header0.time).to(u.s)).value
        fh_ts.seek(0)
        if thread_ids is None:
            thread_ids = list(range(len(fh_raw)))

        super(GSBStreamReader, self).__init__(
            fh_raw, header0=header0, nchan=nchan, bps=bps,
            thread_ids=thread_ids, samples_per_frame=samples_per_frame,
            frames_per_second=frames_per_second)
        self._payloadsize = (self.samples_per_frame // len(fh_raw[0]) *
                             self.nchan * (2 if self._complex_data else 1) *
                             self.bps // 8)
        self._read_frame()

    def read(self, count=None, fill_value=0., squeeze=True, out=None):
        """Read count samples.

        The range retrieved can span multiple frames.

        Parameters
        ----------
        count : int
            Number of samples to read.  If omitted or negative, the whole
            file is read.
        fill_value : float or complex
            Value to use for invalid or missing data.
        squeeze : bool
            If `True` (default), remove channel and thread dimensions if unity.
        out : `None` or array
            Array to store the data in. If given, count will be inferred,
            and squeeze is set to `False`.

        Returns
        -------
        out : array of float or complex
            Dimensions are (sample-time, vlbi-thread, channel).
        """
        if out is None:
            if count is None or count < 0:
                count = self.size - self.offset

            out = np.empty((self.nthread, count, self.nchan),
                           dtype=self._frame.dtype).transpose(1, 0, 2)
        else:
            count = out.shape[0]
            squeeze = False

        offset0 = self.offset
        while count > 0:
            frame_nr, sample_offset = divmod(self.offset,
                                             self.samples_per_frame)
            if(frame_nr != self._frame_nr):
                # Read relevant frame (possibly reusing data array from
                # previous frame set).
                self._read_frame(fill_value, out=self._frame_data)

            if self._frame_data.ndim == 2:
                data = self._frame_data[:, np.newaxis, :]
            else:
                data = self._frame_data.transpose(1, 0, 2)
            # Copy relevant data from frame into output.
            nsample = min(count, self.samples_per_frame - sample_offset)
            sample = self.offset - offset0
            out[sample:sample + nsample] = data[sample_offset:
                                                sample_offset + nsample]
            self.offset += nsample
            count -= nsample

        return out.squeeze() if squeeze else out

    def _read_frame(self, fill_value=0., out=None):
        frame_nr = self.offset // self.samples_per_frame
        self.fh_ts.seek(frame_nr * self._headersize)
        for fh_pair in self.fh_raw:
            for fh in fh_pair:
                fh.seek(frame_nr * self._payloadsize)
        self._frame = GSBFrame.fromfile(self.fh_ts, self.fh_raw,
                                        payloadsize=self._payloadsize,
                                        nchan=self.nchan, bps=self.bps,
                                        complex_data=self._complex_data)
        self._frame_nr = frame_nr
        self._frame_data = self._frame.todata(data=out)
        # Convert payloads to data array.
        return self._frame_data


class GSBStreamWriter(VLBIStreamWriterBase, GSBStreamBase):
    def __init__(self, fh_ts, fh_raw, sample_rate=None,
                 samples_per_frame=1 << 25, nchan=1, bps=4,
                 complex_data=False, header=None, **kwargs):
        self.fh_ts = fh_ts
        if header is None:
            header = GSBHeader.fromvalues(**kwargs)
        frames_per_second = (sample_rate / samples_per_frame).to(u.Hz).value
        super(GSBStreamWriter, self).__init__(
            fh_raw, header0=header, nchan=nchan, bps=bps,
            thread_ids=range(len(fh_raw)),
            samples_per_frame=samples_per_frame,
            frames_per_second=frames_per_second)
        self._data = np.zeros((self.nthread, self.samples_per_frame,
                               self.nchan), (np.complex64 if complex_data
                                             else np.float32))
        self._valid = True

    def write(self, data, squeezed=True):
        """Write data, buffering by frames as needed."""
        if squeezed and data.ndim < 3:
            if self.nthread == 1:
                data = np.expand_dims(data, axis=1)
            if self.nchan == 1:
                data = np.expand_dims(data, axis=-1)

        assert data.shape[1] == self.nthread
        assert data.shape[2] == self.nchan
        assert data.dtype.kind == self._data.dtype.kind

        count = data.shape[0]
        sample = 0
        offset0 = self.offset
        frame = self._data.transpose(1, 0, 2)
        while count > 0:
            frame_nr, sample_offset = divmod(self.offset,
                                             self.samples_per_frame)
            if sample_offset == 0:
                # set up header for new frame.
                time_offset = self.tell(unit=u.s)
                if self.header0.mode == 'phased':
                    full_sub_int = ((frame_nr + self.header0['seq_nr']) * 8 +
                                    self.header0['sub_int'] - 1)
                    self._header = type(self.header0).fromvalues(
                        gps_time=self.header0.gps_time + time_offset,
                        pc_time=self.header0.pc_time + time_offset,
                        seq_nr=full_sub_int // 8,
                        sub_int=(full_sub_int % 8) + 1)
                else:
                    self._header = type(self.header0).fromvalues(
                        time=self.header0.time + time_offset)

            nsample = min(count, self.samples_per_frame - sample_offset)
            sample_end = sample_offset + nsample
            sample = self.offset - offset0
            frame[sample_offset:sample_end] = data[sample:sample + nsample]
            if sample_end == self.samples_per_frame:
                self._frame = GSBFrame.fromdata(self._data, self._header,
                                                self.bps)
                self._frame.tofile(self.fh_ts, self.fh_raw)

            self.offset += nsample
            count -= nsample

    def flush(self):
        self.fh_ts.flush()
        for fh_pair in self.fh_raw:
            for fh in fh_pair:
                fh.flush()


def open(name, mode='rs', **kwargs):
    """Open GSB file(s) for reading or writing.

    Opened as a text file, one gets a standard file handle, but with methods
    to read/write timestamps.  Opened as a binary file, one similarly gets
    methods to read/write a frame.  Opened as a stream, the file is interpreted
    as a timestamp file, but raw files need to be given too. This allows access
    to the stream(s) as series of samples.

    Parameters
    ----------
    name : str
        File name of timestamp or raw data file.
    mode : {'rb', 'wb', 'rt', 'wt', 'rs', or 'ws'}, optional
        Whether to open for reading or writing, and as a regular text or binary
        file (for timestamps and data, respectively) or as a stream (default is
        reading a stream).
    **kwargs
        Additional arguments when opening the file as a stream

    --- For both reading and writing of streams :

    raw : str or (tuple of) tuple of str
        Name of files holding actual data.  For multiple files, the outer
        tuple determines the number of polarisations, and the inner tuple(s)
        the number of streams for each polarisation.  E.g.,
        ((polL1, polL2), (polR1, polR2)).  A single tuple is interpreted as
        two streams of a single polarisation.
    samples_per_frame : int
        Total number of samples per frame.

    --- For reading a stream : (see `~baseband.gsb.base.GSBStreamReader`)

    nchan : int
        Number of channels used to store the data.

    --- For writing a stream : (see `~baseband.gsb.base.GSBStreamWriter`)

    sample_rate : `~astropy.units.Quantity`, optional
        Rate at which samples are given (bandwidth * 2; frequency units).
    header : `~baseband.gsb.GSBHeader`
        Header for the first frame, holding time information, etc.
    **kwargs
        If the header is not given, an attempt will be made to construct one
        with any further keyword arguments.  See
        :class:`~baseband.gsb.base.GSBStreamWriter`.

    Returns
    -------
    Filehandle
        :class:`~baseband.gsb.base.GSBFileReader` or
        :class:`~baseband.gsb.base.GSBFileWriter` instance (binary), or
        :class:`~baseband.gsb.base.GSBStreamReader` or
        :class:`~baseband.gsb.base.GSBStreamWriter` instance (stream)
    """
    if 't' in mode:
        if not hasattr(name, 'read' if 'r' in mode else 'write'):
            name = io.open(name, mode.replace('t', '')+'b')
        elif isinstance(name, io.TextIOBase):
            raise ValueError("Only binary file handles can be used as buffer "
                             "for timestamp files.")
        return GSBTimeStampIO(name)

    if 'b' in mode:
        if 'w' in mode:
            if not hasattr(name, 'write'):
                name = io.open(name, 'wb')
            return GSBFileWriter(name)
        elif 'r' in mode:
            if not hasattr(name, 'read'):
                name = io.open(name, 'rb')
            return GSBFileReader(name)
        else:
            raise ValueError("Only support opening GSB file for reading "
                             "or writing (mode='r' or 'w').")

    # stream mode.
    fh_ts = open(name, mode.replace('s', '') + 't')
    # Single of multiple files.
    raw = kwargs.pop('raw')
    if not isinstance(raw, (list, tuple)):
        raw = ((raw,),)
    # Single of multiple polarisations.
    elif not isinstance(raw[0], (list, tuple)):
        raw = (raw,)
    fh_raw = tuple(tuple(open(p, mode.replace('s', '') + 'b') for p in pol)
                   for pol in raw)
    if 'w' in mode:
        return GSBStreamWriter(fh_ts, fh_raw, **kwargs)
    else:
        return GSBStreamReader(fh_ts, fh_raw, **kwargs)
