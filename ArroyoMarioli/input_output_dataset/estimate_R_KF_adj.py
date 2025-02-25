import sys
from pathlib import Path
import numpy as np
import pandas as pd
import statsmodels.api as sm
import scipy
import warnings

sys.path.append(Path(".").resolve())


def estimate_R(y, gamma, n_start_values_grid=0, maxiter=200):
    """Estimate basic reproduction number using
    Kalman filtering techniques

    Args:
        y (np array): Time series of growth rate in infections
        gamma (double): Rate of recoveries (gamma)
        n_start_values_grid (int, optional): Number of starting values used in the optimization;
            the effective number of starting values is (n_start_values_grid ** 2)
        maxiter (int, optional): Maximum number of iterations

    Returns:
        dict: Dictionary containing the results
          R (np array): Estimated series for R
          se_R (np array): Estimated standard error for R
          flag (int): Optimization flag (0 if successful)
          sigma2_irregular (float): Estimated variance of the irregular component
          sigma2_level (float): Estimated variance of the level component
          gamma (float): Value of gamma used in the estimation

    """
    assert isinstance(
        n_start_values_grid, int
    ), "n_start_values_grid must be an integer"

    assert isinstance(maxiter, int), "maxiter must be an integer"

    assert (
        n_start_values_grid >= 0 and maxiter > 0
    ), "n_start_values_grid and max_iter must be positive"

    assert isinstance(y, np.ndarray), "y must be a numpy array"

    assert y.ndim == 1, "y must be a vector"

    # Setup model instance
    mod_ll = sm.tsa.UnobservedComponents(y, "local level")

    # Estimate model
    if n_start_values_grid > 0:
        # If requested, use multiple starting
        # values for more robust optimization results
        start_vals_grid = (
            np.linspace(0.01, 2.0, n_start_values_grid) * pd.Series(y).var()
        )
        opt_res = []
        for start_val_1 in start_vals_grid:
            for start_val_2 in start_vals_grid:
                res_ll = mod_ll.fit(
                    start_params=np.array([start_val_1, start_val_2]),
                    disp=False,
                    maxiter=maxiter,
                )
                opt_res.append(
                    {
                        "obj_value": res_ll.mle_retvals["fopt"],
                        "start_val_1": start_val_1,
                        "start_val_2": start_val_2,
                        "flag": res_ll.mle_retvals["warnflag"],
                    }
                )
        # The optimizer minimizes the negative of
        # the likelihood, so find the minimum value
        opt_res = pd.DataFrame(opt_res)
        opt_res.sort_values(by="obj_value", ascending=True, inplace=True)
        res_ll = mod_ll.fit(
            start_params=np.array(
                [opt_res["start_val_1"][0], opt_res["start_val_2"][0]]
            ),
            maxiter=maxiter,
            disp=False,
        )
    else:
        res_ll = mod_ll.fit(maxiter=maxiter, disp=False)
    R = 1 + 1 / (gamma) * res_ll.smoothed_state[0]
    se_R = (1 / gamma * (res_ll.smoothed_state_cov[0] ** 0.5))[0]

    # EB:
    print("Look if sd large enough:")
    print(se_R[0:5])

    # EB: added conditional print command
    if res_ll.mle_retvals["converged"] is False:
        print("Estimation did not converge!")

    return {
        "R": R,
        "se_R": se_R,
        "flag": res_ll.mle_retvals["warnflag"],
        "sigma2_irregular": res_ll.params[0],
        "sigma2_level": res_ll.params[1],
        "signal_to_noise": res_ll.params[1] / res_ll.params[0],
        "gamma": gamma,
    }


##############
# Parameters #
##############

min_T = 20
min_signal_to_noise = 1e-3
max_signal_to_noise = 1e2
max_signal_to_noise = 1e15
# EB: changed from 7 to 4
days_infectious = 4  # Baseline for of duration of infectiousness

# EB: added various parameter combinations sourced from other research groups
methods = [
    "_ETH",
    "_RKI",
    "_Ilmenau",
    "_SDSC",
    "_Zi",
    "_AGES",
    "_epiforecasts",
    "_rtlive",
    "_globalrt",
]
gammas = np.reciprocal(np.array([4.8, 4.0, 5.6, 4.8, 5.0, 3.4, 3.6, 4.7, 5.0]))
mean_delays = np.array([11, 1, 7, 10, 0, 0, 12, 12, 0])
parameters = pd.DataFrame({"gamma": gammas, "delay": mean_delays}, index=methods)


# EB: embedded data loading, estimation and saving into a function callable from outside this script
def parametrized_estimation(
    variation, method, data_source, input_folder, output_folder
):
    print(
        "globalrt estimation with "
        + ("parameters" if variation == "" else variation[1:])
        + " from "
        + (
            method[1:]
            if method != ""
            else ("original method" if method != "_window" else "RKI")
        )
        + " and data used by "
        + (data_source[1:] if data_source != "" else "original method"),
    )

    # set parameters for method
    if variation == "_delays":
        gamma = parameters.loc["_globalrt", "gamma"]
    else:
        gamma = parameters.loc[method, "gamma"]

    #############
    # Load data #
    #############

    # EB: deleted clean_folder(output_folder)

    df = pd.read_csv(
        "{}/dataset{}.csv".format(
            input_folder,
            (data_source + "_preprocessed") if variation == "_window" else data_source,
        )
    )
    df["Date"] = pd.to_datetime(df["Date"])

    # EB: added optional date shift to fit delays used by others researchers
    df["Date"] = df["Date"] - pd.DateOffset(
        days=parameters.loc[
            "_globalrt" if variation == "_GTD" or variation == "_window" else method,
            "delay",
        ]
    )

    # Impose minimum time-series observations
    df_temp = (
        df.groupby("Country/Region")
        .count()["gr_infected_{}".format(days_infectious)]
        .reset_index()
    )
    df_temp.rename(
        columns={"gr_infected_{}".format(days_infectious): "no_obs"}, inplace=True
    )
    df = pd.merge(df, df_temp, how="left")
    mask = df["no_obs"] >= min_T
    df = df.loc[
        mask,
    ]

    ##############
    # Estimate R #
    ##############

    df["R"] = np.nan
    df["se_R"] = np.nan

    df_optim_res = []

    with warnings.catch_warnings():
        # Ignore warnings from statsmodels
        # Instead, check later
        warnings.filterwarnings(
            "ignore",
            message="Maximum Likelihood optimization failed to converge. Check mle_retvals",
        )
        for country in df["Country/Region"].unique():
            mask = df["Country/Region"] == country
            df_temp = df.loc[
                mask,
            ].copy()
            y = df_temp["gr_infected_{}".format(days_infectious)].values
            res = estimate_R(y, gamma=gamma)
            df.loc[mask, "R"] = res["R"]
            df.loc[mask, "se_R"] = res["se_R"]
            df_optim_res.append(
                {
                    "Country/Region": country,
                    "flag": res["flag"],
                    "sigma2_irregular": res["sigma2_irregular"],
                    "sigma2_level": res["sigma2_level"],
                    "signal_to_noise": res["signal_to_noise"],
                }
            )
    df_optim_res = pd.DataFrame(df_optim_res)

    # Merge in optimization results
    df = pd.merge(df, df_optim_res, how="left")

    #################################
    # Filter out unreliable results #
    #################################

    # Unsuccessful optimization
    mask = df["flag"] != 0
    df = df.loc[
        ~mask,
    ]

    # Filter out implausible signal-to-noise ratios
    mask = (df["signal_to_noise"] <= min_signal_to_noise) | (
        df["signal_to_noise"] >= max_signal_to_noise
    )
    df = df.loc[
        ~mask,
    ]

    # Collect optimization results
    df_optim_res = (
        df.groupby("Country/Region")
        .first()[["flag", "sigma2_irregular", "sigma2_level", "signal_to_noise"]]
        .reset_index()
    )
    df_optim_res.to_csv(
        "{}/optim_res{}{}{}.csv".format(output_folder, method, variation, data_source),
        index=False,
    )

    ##################
    # Export results #
    ##################

    df = df[["Country/Region", "Date", "R", "se_R"]].copy()
    df.reset_index(inplace=True)
    del df["index"]

    # EB: always using days_infectious = 4
    df["days_infectious"] = 4

    # Calculate confidence intervals
    # EB: changed confidence levels
    alpha = [0.05, 0.5]
    names = ["95", "50"]
    for aa, name in zip(alpha, names):
        t_crit = scipy.stats.norm.ppf(1 - aa / 2)
        df["ci_{}_u".format(name)] = df["R"] + t_crit * df["se_R"]
        df["ci_{}_l".format(name)] = df["R"] - t_crit * df["se_R"]

    # Save estimates
    df.to_csv(
        "{}estimated_R{}{}_data_from{}.csv".format(
            output_folder, method, variation, data_source
        ),
        index=False,
    )
