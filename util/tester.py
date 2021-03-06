import os
import re
from copy import deepcopy

import h5py
import numpy as np
import torch

import data
from data.postprocessor import ImageProcessor
from models.networks import MSECalculator
from util.visualizer import Visualizer, visualize_sidebyside


class Tester:
    def __init__(self, opt, dataset_key='test', visualizer=None):
        self.opt = deepcopy(opt)

        self.opt.serial_batches = True
        self.opt.no_flip = True
        self.opt.isTrain = False
        self.opt.dataset_key = dataset_key

        if 'results_dir' not in self.opt:
            self.opt.results_dir = 'results/'

        self.dataloader = data.create_dataloader(self.opt)

        self.visualizer = Visualizer(self.opt) if visualizer is None else visualizer

        base_path = os.getcwd()
        if self.opt.checkpoints_dir.startswith("./"):
            self.opt.checkpoints_dir = os.path.join(base_path, self.opt.checkpoints_dir[2:])
        else:
            self.opt.checkpoints_dir = os.path.join(base_path, self.opt.checkpoints_dir)

        self.is_validation = self.opt.dataset_key in ["validation", "train"]
        self.N = self.dataloader.dataset.N

        self.results_dir = os.path.join(opt.checkpoints_dir,self.opt.name, self.opt.results_dir, self.opt.dataset_key)
        if not os.path.exists(self.results_dir):
            os.makedirs(self.results_dir)

    def forward(self, model, data_i):
        fake = model.forward(data_i, mode="inference").detach().cpu()
        fake_resized = ImageProcessor.to_255resized_imagebatch(fake, as_tensor=True)
        return fake, fake_resized

    def get_iterator(self, dataloader, indices=None):
        """

        Args:
            indices: a list of indices that should be loaded from dataloader. If it is none, the iterator iterates
                over the entire dataset.

        Returns: iterator

        """
        if indices is None:
            for data_i in dataloader:
                yield data_i
        else:
            for i_val in indices:
                data_i = dataloader.dataset.get_particular(i_val)
                yield data_i

    def _prepare_error_log(self):
        error_log = h5py.File(os.path.join(self.results_dir, f"error_log_{self.opt.dataset_key}.h5"), "w")
        error_log.create_dataset("error", shape=(self.N,), dtype=np.float)
        error_log.create_dataset("user", shape=(self.N,), dtype='S4')
        error_log.create_dataset("filename", shape=(self.N,), dtype='S13')
        error_log.create_dataset("visualisation", shape=(self.N, 1, 380, 1000), dtype=np.uint8)
        return error_log

    def _write_error_log_batch(self, error_log, data_i, i, fake, errors):
        visualisation_data = {**data_i, "fake": fake}
        visuals = visualize_sidebyside(visualisation_data, error_list=errors)

        # We add the entire batch to the output file
        idx_from, idx_to = i * self.opt.batchSize, i * self.opt.batchSize + self.opt.batchSize
        error_log["user"][idx_from:idx_to] = np.array(data_i["user"],
                                                      dtype='S4')
        error_log["filename"][idx_from:idx_to] = np.array(data_i["filename"],
                                                          dtype='S13')
        error_log["error"][idx_from:idx_to] = errors
        vis = np.array([np.copy(v) for k, v in visuals.items()])
        # vis are all floats in [-1, 1]
        vis = (vis + 1) * 128
        error_log["visualisation"][idx_from:idx_to] = vis
        return error_log

    def run_batch(self, data_i, model):
        fake, fake_resized = self.forward(model, data_i)
        target_image = ImageProcessor.as_batch(data_i["target_original"], as_tensor=True)
        errors = np.array(MSECalculator.calculate_mse_for_images(fake_resized, target_image))
        return errors, fake, fake_resized, target_image

    def run_validation(self, model, generator, limit=-1, write_error_log=False):
        print(f"write error log: {write_error_log}")
        assert self.is_validation, "Must be in validation mode"
        if write_error_log:
            error_log = self._prepare_error_log()

        all_errors = list()

        counter = 0
        for i, data_i in enumerate(generator):
            counter += data_i['label'].shape[0]
            if counter > limit:
                break
            if i % 10 == 9:
                print(f"Processing batch {i}")
                print(f"Error so far: {np.sum(all_errors) / len(all_errors) * 1471}")
            errors, fake, fake_resized, target_image = self.run_batch(data_i, model)
            all_errors += list(errors)
            if write_error_log:
                error_log = self._write_error_log_batch(error_log, data_i, i, fake, errors)

        if write_error_log:
            error_log.close()
        return all_errors

    def print_results(self, all_errors, errors_dict, epoch='n.a.', n_steps="n.a."):
        print("Validation Results")
        print("------------------")
        print(f"Error calculated on {len(all_errors)} / {self.dataloader.dataset.N} samples")
        for k in sorted(errors_dict):
            print(f"  {k}, {errors_dict[k]:.2f}")
        print(f"  dataset_key: {self.opt.dataset_key}, model: {self.opt.name}, epoch: {epoch}, n_steps: {n_steps}")

    def run_visual_validation(self, model, mode, epoch, n_steps, limit):
        print(f"Visualizing images for mode '{mode}'...")
        indices = self._get_validation_indices(mode, limit)
        generator = self.get_iterator(self.dataloader, indices=indices)

        result_list = list()
        error_list = list()
        for data_i in generator:
            # data_i = dataloader.dataset.get_particular(i_val)
            errors, fake, fake_resized, target_image = self.run_batch(data_i, model)
            data_i['fake'] = fake
            result_list.append(data_i)
            error_list.append(errors)
        error_list = np.array(error_list)
        error_list = error_list.reshape(-1)
        result = {k: [rl[k] for rl in result_list] for k in result_list[0].keys()}
        for key in ["style_image", "target", "target_original", "fake", "label"]:
            result[key] = torch.cat(result[key], dim=0)

        visuals = visualize_sidebyside(result, log_key=f"{self.opt.dataset_key}/{mode}", w=200, h=320, error_list=error_list)
        self.visualizer.display_current_results(visuals, epoch, n_steps)

    def _get_validation_indices(self, mode, limit):
        if 'rand' in mode:
            validation_indices = self.dataloader.dataset.get_random_indices(limit)
        elif 'fix' in mode:
            # Use fixed validation indices
            validation_indices = self.dataloader.dataset.get_validation_indices()[:limit]
        elif 'full' in mode:
            validation_indices = None
        else:
            raise ValueError(f"Invalid mode: {mode}")
        return validation_indices

    def run(self, model, mode, epoch=None, n_steps=None, limit=-1, write_error_log=False, log=False):
        print(f"Running validation for mode '{mode}'...")
        limit = limit if limit > 0 else self.dataloader.dataset.N
        indices = self._get_validation_indices(mode, limit)
        generator = self.get_iterator(self.dataloader, indices=indices)
        all_errors = self.run_validation(model, generator, limit=limit, write_error_log=write_error_log)

        errors_dict = MSECalculator.calculate_error_statistics(all_errors, mode=mode, dataset_key=self.opt.dataset_key)
        self.print_results(all_errors, errors_dict, epoch, n_steps)

        if log:
            self.log_visualizer(errors_dict, epoch, n_steps)

    def log_visualizer(self, errors_dict, epoch=0, total_steps_so_far=0):
        """

        Args:
            errors_dict: must contain
            epoch:
            total_steps_so_far:
            log_key:

        Returns:

        """
        self.visualizer.print_current_errors(epoch, total_steps_so_far, errors_dict, t=0)
        self.visualizer.plot_current_errors(errors_dict, total_steps_so_far)

    def run_test(self, model, limit=-1):
        filepaths = list()

        for i, data_i in enumerate(self.dataloader):
            if limit > 0 and i * self.opt.batchSize >= limit:
                break
            if i % 10 == 0:
                print(f"Processing batch {i} (processed {self.opt.batchSize * i} images)")

            # The test file names are only 12 characters long, so we have dot to remove
            img_filename = [re.sub(r'\.', '', f) for f in data_i['filename']]

            fake, fake_resized = self.forward(model, data_i)
            # We are testing
            for b in range(len(img_filename)):
                result_path = os.path.join(self.results_dir, img_filename[b] + ".npy")
                assert torch.min(fake_resized[b]) >= 0 and torch.max(fake_resized[b]) <= 255
                np.save(result_path, np.copy(fake_resized[b]).astype(np.uint8))
                filepaths.append(result_path)

        # We are testing
        path_filepaths = os.path.join(self.results_dir, "pred_npy_list.txt")
        with open(path_filepaths, 'w') as f:
            for line in filepaths:
                f.write(line)
                f.write(os.linesep)
        print(f"Written {len(filepaths)} files. Filepath: {path_filepaths}")

    def run_partial_modes(self, model, epoch, n_steps, log, visualize_images, limit):
        # for mode in ['fix', 'rand']:
        for mode in ['rand']:
            self.run(model=model,
                     mode=mode,
                     epoch=epoch,
                     n_steps=n_steps,
                     log=log,
                     limit=limit)
            if visualize_images:
                self.run_visual_validation(model, mode=mode, epoch=epoch,
                                               n_steps=n_steps,
                                               limit=4)


