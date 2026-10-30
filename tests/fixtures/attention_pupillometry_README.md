# Mapping the spatial extent of visual attention with pupillometry

## Contact
A. Researcher, Example University

## Overview
We tested whether background luminance tags reveal the size of the attentional field using pupillometry.
Experiment 1 compared a small with a large attentional field. Experiment 2 compared a ring-shaped
attentional field with a filled disc. For each experiment the repository holds behavioural data, pupil
recordings and the analysis scripts (Python for preprocessing, R for statistics).

## Repository structure
Experiment 1/
    Behavioural_Data/          - .mat files per participant with responses and conditions
        Not_Included/          - .mat files of participants excluded for low accuracy
    Pupil_Data/                - .asc files per participant with pupilsize and gaze position
        Not_included/          - .asc files of participants excluded for low accuracy
    Analysis_Exp1.py           - parsing, baselining and plotting
    LME_Experiment1.R          - mixed-model statistics
Experiment 2/
    Behavioural_Data/
    Pupil_Data/
    Analysis_Exp2.py
    LME_Experiment2.R
README.md

## Reproducing the analysis
Run the Python script first; it writes the preprocessed trial table used by the R script.
