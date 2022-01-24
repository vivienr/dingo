import ast
from typing import Dict, Union

import h5py
import numpy as np
import pandas as pd
from torch.utils.data import Dataset

from dingo.gw.domains import build_domain


class WaveformDataset(Dataset):
    """This class loads a dataset of simulated waveforms (plus and cross
    polarizations, as well as associated parameter values.

    This class loads a stored set of waveforms from an HDF5 file.
    Waveform polarizations are generated by the scripts in
    gw.dataset_generation.
    Once a waveform data set is in memory, the waveform data are consumed through
    a __getitem__() call, optionally applying a chain of transformations, which
    are classes that implement the __call__() method.
    """

    def __init__(self, dataset_file: str, transform=None,
                 single_precision=True):
        """
        Parameters
        ----------
        dataset_file : str
            Load the waveform dataset from this HDF5 file.
        transform :
            Transformations to apply.
        """
        self.transform = transform
        self._Vh = None
        self.single_precision = single_precision
        self.load(dataset_file)


    def __len__(self):
        """The number of waveform samples."""
        return len(self._parameter_samples)


    def __getitem__(self, idx) -> Dict[str, Dict[str, Union[float, np.ndarray]]]:
        """
        Return a nested dictionary containing parameters and waveform polarizations
        for sample with index `idx`. If defined a chain of transformations are being
        applied to the waveform data.
        """
        parameters = self._parameter_samples.iloc[idx].to_dict()
        waveform_polarizations = {'h_cross': self._hc[idx],
                                  'h_plus': self._hp[idx]}
        data = {'parameters': parameters, 'waveform': waveform_polarizations}
        if self._Vh is not None:
            data['waveform']['h_plus'] = data['waveform']['h_plus'] @ self._Vh
            data['waveform']['h_cross'] = data['waveform']['h_cross'] @ self._Vh
        if self.transform:
            data = self.transform(data)
        return data


    def get_info(self):
        """
        Print information on the stored pandas DataFrames.
        This is before any transformations are done.
        """
        self._parameter_samples.info(memory_usage='deep')
        self._hc.info(memory_usage='deep')
        self._hp.info(memory_usage='deep')


    def load(self, filename: str = 'waveform_dataset.h5'):
        """
        Load waveform data set from HDF5 file.

        Parameters
        ----------
        filename : str
            The name of the HDF5 file containing the data set.
        """
        fp = h5py.File(filename, 'r')

        parameter_array = fp['parameters'][:]
        self._parameter_samples = pd.DataFrame(parameter_array)

        if 'waveform_polarizations' in fp:
            grp = fp['waveform_polarizations']  # Backward compatibility; remove later
        else:
            grp = fp['polarizations']
        assert list(grp.keys()) == ['h_cross', 'h_plus']
        self._hc = grp['h_cross'][:]
        self._hp = grp['h_plus'][:]

        if 'rb_matrix_V' in fp.keys():  # Backward compatibility; remove later
            V = fp['rb_matrix_V'][:]
            self._Vh = V.T.conj()
        elif 'svd_V' in fp.keys():
            V = fp['svd_V'][:]
            self._Vh = V.T.conj()

        self.data_settings = ast.literal_eval(fp.attrs['settings'])
        self.domain = build_domain(self.data_settings['domain_settings'])
        self.is_truncated = False

        fp.close()

        # set requested datatype; if dtype is different for _hc/_hp and _Vh,
        # __getitem__() becomes super slow
        dtype = np.complex64 if self.single_precision else np.complex128
        self._hc = np.array(self._hc, dtype=dtype)
        self._hp = np.array(self._hp, dtype=dtype)
        self._Vh = np.array(self._Vh, dtype=dtype)



    def truncate_dataset_domain(self, new_range = None):
        """
        The waveform dataset provides waveforms polarizations in a particular
        range. In uniform Frequency domain for instance, this range is
        [0, domain._f_max]. In practice one may want to apply data conditioning
        different to that of the dataset by specifying a different range,
        and truncating this dataset accordingly. That corresponds to
        truncating the likelihood integral.

        This method provides functionality for that. It truncates the dataset
        to the range specified by the domain, by calling domain.truncate_data.
        In uniform FD, this corresponds to truncating data in the range
        [0, domain._f_max] to the range [domain._f_min, domain._f_max].

        Before this truncation step, one may optionally modify the domain,
        to set a new range. This is done by domain.set_new_range(*new_range),
        which is called if new_range is not None.
        """
        if self.is_truncated:
            raise ValueError('Dataset is already truncated')
        len_domain_original = len(self.domain)

        # optionally set new data range the dataset
        if new_range is not None:
            self.domain.set_new_range(*new_range)

        # truncate the dataset
        if self._Vh is not None:
            assert self._Vh.shape[-1] == len_domain_original, \
                f'Compression matrix Vh with shape {self._Vh.shape} is not ' \
                f'compatible with the domain of length {len_domain_original}.'
            self._Vh = self.domain.truncate_data(
                self._Vh, allow_for_flexible_upper_bound=(new_range is not
                                                          None))
        else:
            raise NotImplementedError('Truncation of the dataset is currently '
                                      'only implemented for compressed '
                                      'polarization data.')

        self.is_truncated = True


