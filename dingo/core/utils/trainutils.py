import time
import os
from os.path import join, isfile
import csv
import numpy as np


class AvgTracker:
    def __init__(self):
        self.sum = 0
        self.N = 0
        self.x = None

    def update(self, x, n=1):
        self.sum += x
        self.N += n
        self.x = x

    def get_avg(self):
        if self.N == 0:
            return float("nan")
        return self.sum / self.N


class LossInfo:
    def __init__(self, epoch, len_dataset, batch_size, mode="Train", print_freq=1):
        # data for print statements
        self.epoch = epoch
        self.len_dataset = len_dataset
        self.batch_size = batch_size
        self.mode = mode
        self.print_freq = print_freq
        # track loss
        self.loss_tracker = AvgTracker()
        self.loss = None
        # track computation times
        self.times = {"Dataloader": AvgTracker(), "Network": AvgTracker()}
        self.t = time.time()

    def update_timer(self, timer_mode="Dataloader"):
        self.times[timer_mode].update(time.time() - self.t)
        self.t = time.time()

    def update(self, loss, n):
        self.loss = loss
        self.loss_tracker.update(loss * n, n)
        self.update_timer(timer_mode="Network")

    def get_avg(self):
        return self.loss_tracker.get_avg()

    def print_info(self, batch_idx):
        if batch_idx % self.print_freq == 0:
            print(
                "{} Epoch: {} [{}/{} ({:.0f}%)]".format(
                    self.mode,
                    self.epoch,
                    min(batch_idx * self.batch_size, self.len_dataset),
                    self.len_dataset,
                    100.0 * batch_idx * self.batch_size / self.len_dataset,
                ),
                end="\t\t",
            )
            # print loss
            print(f"Loss: {self.loss:.3f} ({self.get_avg():.3f})", end="\t\t")
            # print computation times
            td, td_avg = self.times["Dataloader"].x, self.times["Dataloader"].get_avg()
            tn, tn_avg = self.times["Network"].x, self.times["Network"].get_avg()
            print(f"Time Dataloader: {td:.3f} ({td_avg:.3f})", end="\t\t")
            print(f"Time Network: {tn:.3f} ({tn_avg:.3f})")


class RuntimeLimits:
    """
    Keeps track of the runtime limits (time limit, epoch limit, max. number
    of epochs for model).
    """

    def __init__(
        self,
        max_time_per_run: float = None,
        max_epochs_per_run: int = None,
        max_epochs_total: int = None,
        epoch_start: int = None,
    ):
        """

        Parameters
        ----------
        max_time_per_run: float = None
            maximum time for run, in seconds
            [soft limit, break only after full epoch]
        max_epochs_per_run: int = None
            maximum number of epochs for run
        max_epochs_total: int = None
            maximum total number of epochs for model
        epoch_start: int = None
            start epoch of run
        """
        self.max_time_per_run = max_time_per_run
        self.max_epochs_per_run = max_epochs_per_run
        self.max_epochs_total = max_epochs_total
        self.epoch_start = epoch_start
        self.time_start = time.time()
        if max_epochs_per_run is not None and epoch_start is None:
            raise ValueError("epoch_start required to check " "max_epochs_per_run.")

    def limits_exceeded(self, epoch: int = None):
        """
        Check whether any of the runtime limits are exceeded.

        Parameters
        ----------
        epoch: int = None

        Returns
        -------
        limits_exceeded: bool
            flag whether runtime limits are exceeded and run should be stopped;
            if limits_exceeded = True, this prints a message for the reason
        """
        # check time limit for run
        if self.max_time_per_run is not None:
            if time.time() - self.time_start >= self.max_time_per_run:
                print(
                    f"Stop run: Time limit of {self.max_time_per_run} s " f"exceeded."
                )
                return True
        # check epoch limit for run
        if self.max_epochs_per_run is not None:
            if epoch is None:
                raise ValueError("epoch required")
            if epoch - self.epoch_start >= self.max_epochs_per_run:
                print(
                    f"Stop run: Epoch limit of {self.max_epochs_per_run} per run reached."
                )
                return True
        # check total epoch limit
        if self.max_epochs_total is not None:
            if epoch >= self.max_epochs_total:
                print(
                    f"Stop run: Total epoch limit of {self.max_epochs_total} reached."
                )
                return True
        # return False if none of the limits is exceeded
        return False

    def local_limits_exceeded(self, epoch: int = None):
        """
        Check whether any of the local runtime limits are exceeded. Local runtime
        limits include max_epochs_per_run and max_time_per_run, but not max_epochs_total.

        Parameters
        ----------
        epoch: int = None

        Returns
        -------
        limits_exceeded: bool
            flag whether local runtime limits are exceeded
        """
        # check time limit for run
        if self.max_time_per_run is not None:
            if time.time() - self.time_start >= self.max_time_per_run:
                return True
        # check epoch limit for run
        if self.max_epochs_per_run is not None:
            if epoch is None:
                raise ValueError("epoch required")
            if epoch - self.epoch_start >= self.max_epochs_per_run:
                return True
        # return False if none of the limits is exceeded
        return False


def write_history(
    log_dir,
    epoch,
    train_loss,
    test_loss,
    learning_rates,
    aux=None,
    filename="history.txt",
):
    """
    Writes losses and learning rate history to csv file.

    Parameters
    ----------
    log_dir: str
        directory containing the history file
    epoch: int
        epoch
    train_loss: float
        train_loss of epoch
    test_loss: float
        test_loss of epoch
    learning_rates: list
        list of learning rates in epoch
    aux: list = []
        list of auxiliary information to be logged
    filename: str = 'history.txt'
        name of history file
    """
    if aux is None:
        aux = []
    history_file = join(log_dir, filename)
    if epoch == 1:
        assert not isfile(
            history_file
        ), f"File {history_file} exists, aborting to not overwrite it."

    with open(history_file, "w" if epoch == 1 else "a") as f:
        writer = csv.writer(f, delimiter="\t")
        writer.writerow([epoch, train_loss, test_loss, *learning_rates, *aux])


def copyfile(src, dst):
    """
    copy src to dst.
    :param src:
    :param dst:
    :return:
    """
    os.system("cp -p %s %s" % (src, dst))


def save_model(pm, log_dir, model_prefix="model", checkpoint_epochs=None):
    """
    Save model to <model_prefix>_latest.pt in log_dir. Additionally,
    all checkpoint_epochs a permanent checkpoint is saved.

    Parameters
    ----------
    pm:
        model to be saved
    log_dir: str
        log directory, where model is saved
    model_prefix: str = 'model'
        prefix for name of save model
    checkpoint_epochs: int = None
        number of steps between two consecutive model checkpoints
    """
    # save current model
    model_name = join(log_dir, f"{model_prefix}_latest.pt")
    print(f"Saving model to {model_name}.", end=" ")
    pm.save_model(model_name, save_training_info=True)
    print("Done.")

    # potentially copy model to a checkpoint
    if checkpoint_epochs is not None and pm.epoch % checkpoint_epochs == 0:
        model_name_cp = join(log_dir, f"{model_prefix}_{pm.epoch:03d}.pt")
        print(f"Copy model to checkpoint {model_name_cp}.", end=" ")
        copyfile(model_name, model_name_cp)
        print("Done.")


def save_training_injection(outname, pm, data, idx=0):
    """
    For debugging: extract a training injection. To be used inside train or test loop.
    TODO: this function should not really be in core.
    """
    param_names = pm.metadata["train_settings"]["data"]["inference_parameters"]
    mean = pm.metadata["train_settings"]["data"]["standardization"]["mean"]
    std = pm.metadata["train_settings"]["data"]["standardization"]["std"]
    params = {p: data[0][idx, idx_p] for idx_p, p in enumerate(param_names)}
    params = {p: float(v * std[p] + mean[p]) for p, v in params.items()}

    from dingo.gw.domains import build_domain_from_model_metadata

    domain = build_domain_from_model_metadata(pm.metadata)
    detectors = pm.metadata["train_settings"]["data"]["detectors"]
    d = np.array(data[1])
    asds = {
        ifo: 1 / d[idx, idx_ifo, 2] * 1e-23 for idx_ifo, ifo in enumerate(detectors)
    }
    strains = {
        ifo: (d[idx, idx_ifo, 0] + 1j * d[idx, idx_ifo, 1])
        * (asds[ifo] * domain.noise_std)
        for idx_ifo, ifo in enumerate(detectors)
    }

    out_data = {"parameters": params, "asds": asds, "strains": strains}
    np.save(outname, out_data)

    from dingo.gw.inference.injection import GWSignal

    signal = GWSignal(
        pm.metadata["dataset_settings"]["waveform_generator"],
        domain,
        domain,
        pm.metadata["train_settings"]["data"]["detectors"],
        pm.metadata["train_settings"]["data"]["ref_time"],
    )
    params_2 = params.copy()
    params_2["phase"] = (params_2["phase"] + np.pi/2.) % (2 * np.pi)
    params_3 = {p: v * 0.99 for p, v in params.items()}
    sample = signal.signal(params)
    sample_2 = signal.signal(params_2)
    sample_3 = signal.signal(params_3)

    import matplotlib.pyplot as plt

    plt.plot(np.abs(sample["waveform"]["H1"])[domain.min_idx:])
    plt.plot(np.abs(strains["H1"]), lw=0.8)
    plt.show()

    plt.plot(sample["waveform"]["H1"][domain.min_idx:])
    plt.plot(strains["H1"], lw=0.8)
    # plt.plot(sample_2["waveform"]["H1"][domain.min_idx:])
    plt.show()
