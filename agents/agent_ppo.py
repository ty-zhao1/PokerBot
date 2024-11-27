import torch
import torch.nn as nn
import torch.optim as optim
import numpy as np
from torch.distributions import Categorical
from gym_env.enums import Action, Stage
import random

class PPOActorCritic(nn.Module):
    """Combined Actor-Critic Network."""
    def __init__(self, state_size, action_size):
        super(PPOActorCritic, self).__init__()
        # Shared layers
        self.shared_fc1 = nn.Linear(state_size, 64)
        self.shared_fc2 = nn.Linear(64, 64)

        # Actor network
        self.actor_fc = nn.Linear(64, action_size)

        # Critic network
        self.critic_fc = nn.Linear(64, 1)
        
        for layer in self.modules():
            if isinstance(layer, nn.Linear):
                nn.init.xavier_uniform_(layer.weight)
                nn.init.zeros_(layer.bias)

    def forward(self, state):
        x = torch.relu(self.shared_fc1(state))
        x = torch.relu(self.shared_fc2(x))
        action_logits = self.actor_fc(x)
        value = self.critic_fc(x)
        return action_logits, value


class Player:
    def __init__(self, env, state_size, action_size, lr=1e-3, gamma=0.99, clip_epsilon=0.2, entropy_coeff=0.001, critic_coeff=0.5):
        self.env = env
        self.state_size = state_size
        self.action_size = action_size
        self.gamma = gamma
        self.clip_epsilon = clip_epsilon
        self.entropy_coeff = entropy_coeff
        self.critic_coeff = critic_coeff

        # Initialize actor-critic network
        self.model = PPOActorCritic(state_size, action_size)
        self.optimizer = optim.Adam(self.model.parameters(), lr=lr)

        # Storage for trajectories
        self.states = []
        self.actions = []
        self.log_probs = []
        self.rewards = []
        self.dones = []

    def action(self, action_space, observation, info):
        """
        Mandatory method that calculates the move based on the observation array and the action space.
        """
        _ = observation  # Not using the observation for random decision
        _ = info

        # Define all possible player actions
        this_player_action_space = {
            Action.FOLD, Action.CHECK, Action.CALL, 
            Action.RAISE_POT, Action.RAISE_HALF_POT, Action.RAISE_2POT
        }

        # Intersect with the action_space to get valid actions
        allowed_actions = list(this_player_action_space.intersection(set(action_space)))

        # Remove SHOWDOWN from allowed actions if present
        if Stage.SHOWDOWN in allowed_actions:
            allowed_actions.remove(Stage.SHOWDOWN)
        if Stage.SHOWDOWN.value in allowed_actions:
            allowed_actions.remove(Stage.SHOWDOWN.value)
        
        if not allowed_actions:
            # Handle the edge case when no valid actions are available
            return random.choice(range(self.action_size)), torch.tensor(0.0)

        # Convert observation to tensor
        # print(observation)
        state = torch.FloatTensor(observation).unsqueeze(0)
        state = torch.nan_to_num(state, nan=0.0)
        # print(state)
        if torch.isnan(state).any():
            raise Exception("NaN detected in state")
        # Get action logits and state value from the policy network
        with torch.no_grad():
            action_logits, _ = self.model(state)

        # Filter logits to only include valid actions
        mask = torch.full((self.action_size,), -np.inf)  # Start with -inf for all actions
        for action in allowed_actions:
            mask[action.value] = 0  # Allow valid actions
        
        # print(action_logits)
        # Apply the mask to the logits
        masked_logits = action_logits.squeeze() + mask
        
        # Check for NaNs in masked_logits
        if torch.isnan(action_logits).any():
            raise Exception("NaN detected in action_logits")
        # print(masked_logits)
        # Create a probability distribution over the valid actions
        probs = torch.softmax(masked_logits, dim=-1)
        dist = Categorical(probs)

        # Sample an action from the distribution
        sampled_action = dist.sample()

        # Get the log probability of the sampled action
        log_prob = dist.log_prob(sampled_action)

        # Return the sampled action and its log probability
        return sampled_action.item(), log_prob

    def store_transition(self, state, action, log_prob, reward, done):
        """Store transitions for training."""
        self.states.append(state)
        self.actions.append(action)
        self.log_probs.append(log_prob)
        self.rewards.append(reward)
        self.dones.append(done)

    def compute_returns_and_advantages(self, values):
        """Compute discounted returns and advantages using GAE."""
        returns = []
        advantages = []
        gae = 0
        next_value = 0

        for t in reversed(range(len(self.rewards))):
            delta = self.rewards[t] + self.gamma * next_value * (1 - self.dones[t]) - values[t]
            gae = delta + self.gamma * 0.95 * gae * (1 - self.dones[t])
            advantages.insert(0, gae)
            next_value = values[t]
        returns = [adv + val for adv, val in zip(advantages, values)]
        return returns, advantages

    def learn(self):
        """Perform one training iteration."""
        states = torch.FloatTensor(self.states)
        actions = torch.LongTensor(self.actions)
        old_log_probs = torch.stack(self.log_probs)
        dones = torch.FloatTensor(self.dones)
        states = torch.nan_to_num(states, nan=0.0)
        # print(states)
        if torch.isnan(states).any():
            raise Exception("NaN detected in states in learn")
        # Get values and action logits
        action_logits, values = self.model(states)
        if torch.isnan(action_logits).any():
            raise Exception("NaN detected in action_logits in learn")
        dist = Categorical(torch.softmax(action_logits, dim=-1))
        log_probs = dist.log_prob(actions)
        entropy = dist.entropy()

        # Compute returns and advantages
        values = values.squeeze()
        returns, advantages = self.compute_returns_and_advantages(values)

        # Convert to tensors
        returns = torch.FloatTensor(returns)
        advantages = torch.FloatTensor(advantages)

        # PPO loss
        ratios = torch.exp(log_probs - old_log_probs.detach())
        # if log_probs - old_log_probs is below a threshold, use first order taylor expansion
        # ratios_temp = log_probs - old_log_probs.detach()
        if torch.isnan(ratios).any():
            raise Exception("NaN detected in ratios_temp")
        surr1 = ratios * advantages
        surr2 = torch.clamp(ratios, 1 - self.clip_epsilon, 1 + self.clip_epsilon) * advantages
        actor_loss = -torch.min(surr1, surr2).mean()
        critic_loss = (returns - values).pow(2).mean()
        entropy_loss = -entropy.mean()

        loss = actor_loss + self.critic_coeff * critic_loss + self.entropy_coeff * entropy_loss

        # Backpropagation
        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()

        # Clear trajectories
        self.states = []
        self.actions = []
        self.log_probs = []
        self.rewards = []
        self.dones = []
    # def learn(self):
    #     """Perform one training iteration."""
    #     states = torch.FloatTensor(self.states)
    #     actions = torch.LongTensor(self.actions)
    #     old_log_probs = torch.stack(self.log_probs)
    #     dones = torch.FloatTensor(self.dones)

    #     # Get values and action logits
    #     action_logits, values = self.model(states)
    #     values = values.squeeze()

    #     # Mask invalid actions (if needed during learning)
    #     # Not necessary here if actions are already valid

    #     dist = Categorical(torch.softmax(action_logits, dim=-1))
    #     log_probs = dist.log_prob(actions)
    #     entropy = dist.entropy()

    #     # Compute returns and advantages
    #     returns, advantages = self.compute_returns_and_advantages(values.detach().numpy())
    #     returns = torch.FloatTensor(returns)
    #     advantages = torch.FloatTensor(advantages)
    #     advantages = (advantages - advantages.mean()) / (advantages.std() + 1e-8)

    #     # PPO loss
    #     ratios = torch.exp(log_probs - old_log_probs.detach())
    #     surr1 = ratios * advantages
    #     surr2 = torch.clamp(ratios, 1 - self.clip_epsilon, 1 + self.clip_epsilon) * advantages
    #     actor_loss = -torch.min(surr1, surr2).mean()
    #     critic_loss = self.critic_coeff * (returns - values).pow(2).mean()
    #     entropy_loss = self.entropy_coeff * entropy.mean()

    #     loss = actor_loss + critic_loss - entropy_loss

    #     # Check for NaNs
    #     if torch.isnan(loss):
    #         raise Exception("NaN detected in loss. Stopping training.")

    #     # Backpropagation
    #     self.optimizer.zero_grad()
    #     loss.backward()
    #     torch.nn.utils.clip_grad_norm_(self.model.parameters(), max_norm=0.5)
    #     self.optimizer.step()

    #     # Clear trajectories
    #     self.states = []
    #     self.actions = []
    #     self.log_probs = []
    #     self.rewards = []
    #     self.dones = []


    def train(self, episodes=10):
        """Train the agent."""
        for episode in range(episodes):
            state = self.env.reset()
            episode_reward = 0
            done = False
            while not done:
                action, log_prob = self.action(self.env.legal_moves, state, None)
                next_state, reward, done, _ = self.env.step(action)

                self.store_transition(state, action, log_prob, reward, done)
                state = next_state
                episode_reward += reward

            # Learn from the episode
            _, values = self.model(torch.FloatTensor(self.states))
            values = values.squeeze().detach().numpy()
            self.learn()

            print(f"Episode {episode + 1}/{episodes}, Reward: {episode_reward}")
