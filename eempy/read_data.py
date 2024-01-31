"""
Functions for importing raw EEM data
Author: Yongmin Hu (yongminhu@outlook.com)
Last update: 2024-01-15
"""

import os
import re
import numpy as np
import pandas as pd
from datetime import datetime, timedelta


def read_eem(filepath, data_format='aqualog'):
    """import EEM from file. Due to the differences between EEM files generated by different instruments, the type of
    data_format must be specified. The current version only supports importing Aqualog (HORIBA) files, which are named
    using the format "xxPEM.dat" by default. The blank file generate by aqualog ('xxBEM.dat') can also be read with
    this function.

    Parameters
    ----------------
    filepath: str
        the filepath to the aqualog EEM file
    data_format: str
        specify the type of EEM data format

    Returns
    ----------------
    intensity: np.ndarray (2d)
        the EEM matrix
    ex_range: np.ndarray (1d)
        the excitation wavelengths
    em_range: np.ndarray (1d)
        the emission wavelengths
    """
    with open(filepath, 'r') as of:
        if data_format == 'aqualog':
            # get header (the first line in this case the Ex wavelength)
            firstline = of.readline()

            # remove unwanted characters
            firstline = re.findall(r"\d+", firstline)
            header = np.array([list(map(int, firstline))])

            # get index (the first column, in this case the Em wavelength) and the eem matrix
            index = []
            data = np.zeros(np.shape(header))
            line = of.readline()  # start reading from the second line
            while line:
                initial = (line.split())[0]
                # check if items only contains digits
                try:
                    initial = float(initial)
                    index.append(initial)
                    # get fluorescence intensity data from each line
                    dataline = np.array([list(map(float, (line.split())[1:]))])
                    try:
                        data = np.concatenate([data, dataline])
                    except ValueError:
                        print('please check the consistancy of header and data dimensions:\n')
                        print('number of columns suggested by your header: ', np.size(data), '\n')
                        print('number of columns you have in your intensity data: ', np.size(dataline))
                        break
                except ValueError:
                    pass
                line = of.readline()
            of.close()
            index = np.array(list(map(float, index)))
            data = data[1:, :]

        # Transpose the data matrix to set Xaxis-Em and Yaxis-Ex due to the fact
        # that the wavelength range of Em is larger, and it is visually better to
        # set the longer axis horizontally.
        intensity = data.T
        em_range = index
        ex_range = header[0]
        if em_range[0] > em_range[1]:
            em_range = np.flipud(em_range)
        if ex_range[0] > ex_range[1]:
            ex_range = np.flipud(ex_range)
        else:
            Warning(
                'The current version of eempy only supports reading files written in the "Aqualog (HORIBA) format."')
    return intensity, ex_range, em_range


def read_abs(filepath, data_format='aqualog'):
    """import UV absorbance data from aqualog UV absorbance file. This kind of files are named using the format
    "xxABS.dat" by the aqualog software by default.

    Parameters
    ----------------
    filepath: str
        the filepath to the UV absorbance file
    data_format: str
        specify the type of UV absorbance data format

    Returns
    ----------------
    absorbance:np.ndarray (1d)
        the UV absorbance spectra
    ex_range: np.ndarray (1d)
        the excitation wavelengths
    """
    with open(filepath, 'r') as of:
        if data_format == 'aqualog':
            line = of.readline()
            index = []
            data = []
            while line:
                initial = float((line.split())[0])
                index.append(initial)
                try:
                    value = float((line.split())[1])
                    data.append(value)
                except IndexError:
                    data.append(np.nan)
                    # if empty, set the value to nan
                line = of.readline()
            of.close()
            ex_range = np.flipud(index)
            absorbance = np.flipud(data)
        else:
            Warning(
                'The current version of eempy only supports reading files written in the "Aqualog (HORIBA) format."')
    return absorbance, ex_range


def read_reference_from_text(filepath):
    """Read reference data from text file. The reference data can be any 1D data (e.g., dissolved organic carbon
    concentration). This first line of the file should be a header, and then each following line contains one datapoint,
     without any separators other than line breaks.
    For example->
    '''
    DOC (mg/L)
    1.0
    2.5
    4.8
    '''

    Parameters
    ----------------
    filepath: str
        the filepath to the aqualog UV absorbance file

    Returns
    ----------------
    absorbance:np.ndarray (1d)
        the reference data
    header: str
        the header
    """
    reference_data = []
    with open(filepath, "r") as f:
        line = f.readline()
        header = line.split()[0]
        while line:
            try:
                line = f.readline()
                reference_data.append(float(line.split()[0]))
            except IndexError:
                pass
        f.close()
    return reference_data, header


def get_filelist(filedir, kw):
    """
    get a list containing all filenames with a given keyword in a folder
    For example, this can be used for searching EEM files (with the keyword "PEM.dat")
    """
    filelist = os.listdir(filedir)
    datlist = [file for file in filelist if kw in file]
    return datlist


# def ts_reshape(ts, timezone_correction=1):
#     ts_reshaped = ts.drop_duplicates(subset=['time'], keep='last')
#     ts_reshaped["time"] = pd.to_datetime(ts_reshaped.time) + timedelta(hours=timezone_correction)
#     ts_reshaped = ts_reshaped.set_index("time")
#     return ts_reshaped


def read_parafac_model(filepath):
    """
    Import PARAFAC model from a text file written in the format suggested by OpenFluor (
    https://openfluor.lablicate.com/). Note that the models downloaded from OpenFluor normally don't have scores.

    Parameters
    ----------------
    filepath: str
        the filepath to the aqualog UV absorbance file

    Returns
    ----------------
    ex_df: pd.DataFrame
        excitation loadings
    em_df: pd.DataFrame
        emission loadings
    score_df: pd.DataFrame or None
        scores (if there's any)
    info_dict: dict
        a dictionary containing the model information
    """
    with open(filepath, 'r') as f:
        line = f.readline().strip()
        line_count = 0
        while '#' in line:
            if "Fluorescence" in line:
                print("Reading fluorescence measurement info...")
            line = f.readline().strip()
            line_count += 1
        info_dict = {}
        while '#' not in line:
            phrase = line.split(sep='\t')
            if len(phrase) > 1:
                info_dict[phrase[0]] = phrase[1]
            else:
                info_dict[phrase[0]] = ''
            line = f.readline().strip()
            line_count += 1
        while '#' in line:
            if "Excitation" in line:
                print("Reading Ex/Em loadings...")
            line = f.readline().strip()
            line_count_spectra_start = line_count
            line_count += 1
        while "Ex" in line:
            line = f.readline().strip()
            line_count += 1
        line_count_ex = line_count
        ex_df = pd.read_csv(filepath, sep="\t", header=None, index_col=[0, 1],
                            skiprows=line_count_spectra_start + 1, nrows=line_count_ex - line_count_spectra_start - 1)
        component_label = ['component {rank}'.format(rank=r + 1) for r in range(ex_df.shape[1])]
        ex_df.columns = component_label
        ex_df.index.names = ['type', 'wavelength']
        while "Em" in line:
            line = f.readline().strip()
            line_count += 1
        line_count_em = line_count
        em_df = pd.read_csv(filepath, sep='\t', header=None, index_col=[0, 1],
                            skiprows=line_count_ex, nrows=line_count_em - line_count_ex)
        em_df.columns = component_label
        em_df.index.names = ['type', 'wavelength']
        score_df = None
        while '#' in line:
            if "Score" in line:
                print("Reading component scores...")
            line = f.readline().strip()
            line_count += 1
        line_count_score = line_count
        while 'Score' in line:
            line = f.readline().strip()
            line_count += 1
        while '#' in line:
            if 'end' in line:
                line_count_end = line_count
                score_df = pd.read_csv(filepath, sep="\t", header=None, index_col=[0, 1],
                                       skiprows=line_count_score, nrows=line_count_end - line_count_score)
                score_df.index = score_df.index.set_levels(
                    [score_df.index.levels[0], pd.to_datetime(score_df.index.levels[1])])
                score_df.columns = component_label
                score_df.index.names = ['type', 'time']
                print('Reading complete')
                line = f.readline().strip()
        f.close()
    return ex_df, em_df, score_df, info_dict


def read_parafac_models(datdir, kw):
    """
    Search all PARAFAC models in a folder by keyword in filenames and import all of them into a dictionary using
    read_parafac_model()
    """
    datlist = get_filelist(datdir, kw)
    parafac_results = []
    for f in datlist:
        filepath = datdir + '/' + f
        ex_df, em_df, score_df, info_dict = read_parafac_model(filepath)
        info_dict['filename'] = f
        d = {'info': info_dict, 'ex': ex_df, 'em': em_df, 'score': score_df}
        parafac_results.append(d)
    return parafac_results


def get_timestamp_from_filename(filename, ts_format='%Y-%m-%d-%H-%M-%S', ts_start_position=0, ts_end_position=19):
    ts_string = filename[ts_start_position:ts_end_position]
    ts = datetime.strptime(ts_string, ts_format)
    return ts