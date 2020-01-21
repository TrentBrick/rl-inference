# pylint: disable=not-callable
# pylint: disable=no-member

import torch
import torch.nn as nn

from .measures import InformationGain


class Planner(nn.Module):
    def __init__(
        self,
        ensemble,
        reward_model,
        action_size,
        plan_horizon,
        optimisation_iters,
        n_candidates,
        top_candidates,
        use_reward=True,
        use_exploration=True,
        expl_scale=1,
        device="cpu",
    ):
        super().__init__()
        self.ensemble = ensemble
        self.reward_model = reward_model
        self.action_size = action_size
        self.ensemble_size = ensemble.ensemble_size

        self.plan_horizon = plan_horizon
        self.optimisation_iters = optimisation_iters
        self.n_candidates = n_candidates
        self.top_candidates = top_candidates

        self.use_reward = use_reward
        self.use_exploration = use_exploration
        self.expl_scale = expl_scale
        self.device = device

        self.measure = InformationGain(self.ensemble, self.expl_scale)
        self.reward_stats = []

    def get_stats(self):
        if self.use_exploration:
            info_stats = self.measure.get_stats()
        else:
            info_stats = {}
        reward_tensor = torch.stack(self.reward_stats)
        reward_tensor = reward_tensor.view(-1)

        reward_stats = {
            "max": reward_tensor.max().item(),
            "mean": reward_tensor.mean().item(),
            "min": reward_tensor.min().item(),
            "std": reward_tensor.std().item(),
        }

        self.reward_stats = []
        return info_stats, reward_stats

    def forward(self, state):
        state = torch.from_numpy(state).float().to(self.device)
        state_size = state.size(0)
        state = state.unsqueeze(dim=0).unsqueeze(dim=0)
        state = state.repeat(self.ensemble_size, self.n_candidates, 1)

        action_mean = torch.zeros(self.plan_horizon, 1, self.action_size).to(
            self.device
        )
        action_std_dev = torch.ones(self.plan_horizon, 1, self.action_size).to(
            self.device
        )

        for _ in range(self.optimisation_iters):
            actions = action_mean + action_std_dev * torch.randn(
                self.plan_horizon,
                self.n_candidates,
                self.action_size,
                device=self.device,
            )

            states, delta_vars, delta_means = self.perform_rollout(state, actions)
            returns = torch.zeros(self.n_candidates).float().to(self.device)

            if self.use_exploration:
                expl_bonus = self.measure(delta_means, delta_vars) * self.expl_scale
                expl_bonus = expl_bonus.sum(dim=0)
                returns += expl_bonus

            if self.use_reward:
                states = states.view(-1, state_size)
                rewards = self.reward_model(states)

                rewards = rewards.view(
                    self.plan_horizon, self.ensemble_size, self.n_candidates
                )

                rewards = rewards.mean(dim=1).sum(dim=0)
                self.reward_stats.append(rewards)
                returns += rewards

            returns = torch.where(
                torch.isnan(returns), torch.zeros_like(returns), returns
            )

            _, topk = returns.topk(
                self.top_candidates, dim=0, largest=True, sorted=False
            )

            best_actions = actions[:, topk.view(-1)].reshape(
                self.plan_horizon, self.top_candidates, self.action_size
            )

            action_mean, action_std_dev = (
                best_actions.mean(dim=1, keepdim=True),
                best_actions.std(dim=1, unbiased=False, keepdim=True),
            )

        return action_mean[0].squeeze(dim=0)

    def perform_rollout(self, current_state, actions):
        T = self.plan_horizon + 1
        states = [torch.empty(0)] * T
        delta_means = [torch.empty(0)] * T
        delta_vars = [torch.empty(0)] * T
        states[0] = current_state

        actions = actions.unsqueeze(0)
        actions = actions.repeat(self.ensemble_size, 1, 1, 1).permute(1, 0, 2, 3)

        for t in range(self.plan_horizon):
            delta_mean, delta_var = self.ensemble(states[t], actions[t])
            states[t + 1] = states[t] + self.ensemble.sample(delta_mean, delta_var)
            # states[t + 1] = mean
            delta_means[t + 1] = delta_mean
            delta_vars[t + 1] = delta_var

        states = torch.stack(states[1:], dim=0)
        delta_vars = torch.stack(delta_vars[1:], dim=0)
        delta_means = torch.stack(delta_means[1:], dim=0)
        return states, delta_vars, delta_means
