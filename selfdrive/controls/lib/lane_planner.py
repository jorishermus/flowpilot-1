# hard-forked from https://github.com/commaai/openpilot/tree/05b37552f3a38f914af41f44ccc7c633ad152a15/selfdrive/controls/lib/lane_planner.py
import numpy as np
import statistics
from cereal import log
from common.filter_simple import FirstOrderFilter
from common.numpy_fast import interp
from common.realtime import DT_MDL
from system.swaglog import cloudlog
from common.logger import sLogger

# positive numbers go right, negative go left
TRAJECTORY_SIZE = 33
PATH_OFFSET = 0.225
CAMERA_OFFSET = 0.225

# <0.5 to be on the right side of the road
DEFAULT_LANE_CENTERING = 0.375
MAX_EDGE_DISTANCE = 10
MIN_EDGE_DISTANCE = 1.8

def lerp(a, b, t):
  if t >= 1.0:
    return b
  if t <= 0.0:
    return a

  return (b * t) + (a * (1.0 - t))

def clamp(num, min_value, max_value):
  return max(min(num, max_value), min_value)

class LanePlanner:
  def __init__(self, wide_camera=False):
    self.ll_t = np.zeros((TRAJECTORY_SIZE,))
    self.ll_x = np.zeros((TRAJECTORY_SIZE,))
    self.lll_y = np.zeros((TRAJECTORY_SIZE,))
    self.rll_y = np.zeros((TRAJECTORY_SIZE,))
    self.lane_width_estimate = FirstOrderFilter(3.1, 9.95, DT_MDL)
    self.lane_width_certainty = FirstOrderFilter(1.0, 0.95, DT_MDL)
    self.lane_width = 3.7

    self.lll_prob = 0.
    self.rll_prob = 0.
    self.final_lane_plan_factor = 1.0

    self.lle_y = np.zeros((TRAJECTORY_SIZE,))
    self.rle_y = np.zeros((TRAJECTORY_SIZE,))
    self.lle_std = 0.
    self.rle_std = 0.
    self.lle_stds = []
    self.rle_stds = []
    self.rle_std_avg = 0.
    self.lle_std_avg = 0.
    self.lle_y_dists = []
    self.rle_y_dists = []
    self.road_width = 7.0
    self.on_right_side = True

    self.lll_std = 0.
    self.rll_std = 0.

    self.l_lane_change_prob = 0.
    self.r_lane_change_prob = 0.

    self.camera_offset = -CAMERA_OFFSET if wide_camera else CAMERA_OFFSET
    self.path_offset = -PATH_OFFSET if wide_camera else PATH_OFFSET

  def parse_model(self, md):
    lane_lines = md.laneLines
    edges = md.roadEdges

    if len(edges[0].t) == TRAJECTORY_SIZE:
      self.lle_std = md.roadEdgeStds[0]
      self.rle_std = md.roadEdgeStds[1]
      # average out edge deviations
      self.lle_stds.append(self.lle_std)
      self.rle_stds.append(self.rle_std)
      if len(self.lle_stds) > 5:
        self.lle_stds.pop(0)
        self.rle_stds.pop(0)
      self.lle_std_avg = statistics.fmean(self.lle_stds)
      self.rle_std_avg = statistics.fmean(self.rle_stds)
      # get more reliable edge in fuzzy, high std scenarios
      self.lle_y = np.array(edges[0].y) + self.camera_offset + self.lle_std_avg * 0.9
      self.rle_y = np.array(edges[1].y) + self.camera_offset - self.rle_std_avg * 0.9

    if len(lane_lines) == 4 and len(lane_lines[0].t) == TRAJECTORY_SIZE:
      self.ll_t = (np.array(lane_lines[1].t) + np.array(lane_lines[2].t))/2
      # left and right ll x is the same
      self.ll_x = lane_lines[1].x
      self.lll_y = np.array(lane_lines[1].y) + self.camera_offset
      self.rll_y = np.array(lane_lines[2].y) + self.camera_offset
      self.lll_prob = md.laneLineProbs[1]
      self.rll_prob = md.laneLineProbs[2]
      self.lll_std = md.laneLineStds[1]
      self.rll_std = md.laneLineStds[2]

    desire_state = md.meta.desireState
    if len(desire_state):
      self.l_lane_change_prob = desire_state[log.LateralPlan.Desire.laneChangeLeft]
      self.r_lane_change_prob = desire_state[log.LateralPlan.Desire.laneChangeRight]

  def get_d_path(self, CS, v_ego, path_t, path_xyz):
    # set path offset
    path_xyz[:, 1] += self.path_offset

    # model lane probability boost
    l_prob = clamp(self.lll_prob * 4.0, 0.0, 1.0)
    r_prob = clamp(self.rll_prob * 4.0, 0.0, 1.0)

    # Reduce reliance on uncertain lanelines, but have a wide range
    l_prob *= interp(self.lll_std, [.3, .8], [1.0, 0.0])
    r_prob *= interp(self.rll_std, [.3, .8], [1.0, 0.0])

    # Find current lanewidth
    self.lane_width_certainty.update(l_prob * r_prob)
    current_lane_width = abs(self.rll_y[0] - self.lll_y[0])
    self.lane_width_estimate.update(current_lane_width)
    speed_lane_width = interp(v_ego, [0., 31.], [2.8, 3.5])
    self.lane_width = lerp(speed_lane_width, self.lane_width_estimate.x, self.lane_width_certainty.x)

    clipped_lane_width = min(4.0, self.lane_width)

    # lane paths
    lane_path_prob = max(l_prob, r_prob)
    path_from_left_lane = self.lll_y + clipped_lane_width / 2.0
    path_from_right_lane = self.rll_y - clipped_lane_width / 2.0

    # track how far on average we are from the road edges
    # store the last few readings for averaging
    # only if we see lanelines OR steering
    # clear distances if we are changing lanes
    if self.final_lane_plan_factor < 1.0:
      self.rle_y_dists.clear()
      self.lle_y_dists.clear()
      self.lle_stds.clear()
      self.rle_stds.clear()
    elif lane_path_prob > 0.5 or CS.steeringPressed:
      # add clamped edge distances if we have some confidence in it
      if self.rle_std_avg < 1.0:
        self.rle_y_dists.append(clamp(self.rle_y[0],  MIN_EDGE_DISTANCE,  MAX_EDGE_DISTANCE))
      else:
        self.rle_y_dists.append(self.road_width * DEFAULT_LANE_CENTERING)
      if self.lle_std_avg < 1.0:
        self.lle_y_dists.append(clamp(self.lle_y[0], -MAX_EDGE_DISTANCE, -MIN_EDGE_DISTANCE))
      else:
        self.lle_y_dists.append(self.road_width * -(1.0 - DEFAULT_LANE_CENTERING))

      # only store the last few seconds
      if len(self.lle_y_dists) > 120:
        self.lle_y_dists.pop(0)
      if len(self.rle_y_dists) > 120:
        self.rle_y_dists.pop(0)

    # get average distances from edges
    left_edge_dist = statistics.fmean(self.lle_y_dists) if len(self.lle_y_dists) > 0 else -MAX_EDGE_DISTANCE
    right_edge_dist = statistics.fmean(self.rle_y_dists) if len(self.rle_y_dists) > 0 else MAX_EDGE_DISTANCE

    if left_edge_dist > -MAX_EDGE_DISTANCE and right_edge_dist < MAX_EDGE_DISTANCE:
      self.road_width = right_edge_dist - left_edge_dist

    # which edge are we going to follow?
    right_is_closer = abs(right_edge_dist - self.rle_y[0]) < abs(left_edge_dist - self.lle_y[0])
    if self.on_right_side and self.rle_std_avg > self.lle_std_avg and not right_is_closer and left_edge_dist > -MAX_EDGE_DISTANCE:
      self.on_right_side = False
    elif not self.on_right_side and self.rle_std_avg < self.lle_std_avg and right_is_closer and right_edge_dist < MAX_EDGE_DISTANCE:
      self.on_right_side = True

    path_from_edges = None if left_edge_dist <= -MAX_EDGE_DISTANCE and right_edge_dist >= MAX_EDGE_DISTANCE or min(self.rle_std_avg, self.lle_std_avg) > 1.5 else self.lle_y - left_edge_dist if not self.on_right_side else self.rle_y - right_edge_dist

    # ok, mix all this together based on lane probability
    lane_path_y = (l_prob * path_from_left_lane + r_prob * path_from_right_lane) / (l_prob + r_prob + 0.0001)
    final_path_y = lerp(path_from_edges, lane_path_y, lane_path_prob * 2.0) if path_from_edges is not None else lane_path_y

    # if we have no edge path, we rely on the reliability of the lane paths
    if path_from_edges is None:
      self.final_lane_plan_factor *= clamp(lane_path_prob * 2.0, 0.0, 1.0)

    #debug
    sLogger.Send("0lP" + "{:.2f}".format(l_prob) + " rP" + "{:.2f}".format(r_prob) +
                " lX" + "{:.1f}".format(self.lll_y[0]) + " rX" + "{:.1f}".format(self.rll_y[0]) +
                " leX" + "{:.1f}".format(self.lle_y[0]) + " reX" + "{:.1f}".format(self.rle_y[0]) +
                " ls" + "{:.2f}".format(self.lll_std) + " rs" + "{:.2f}".format(self.rll_std) +
                " w" + "{:.1f}".format(self.lane_width) + " ld" + "{:.1f}".format(left_edge_dist) +
                " rd" + "{:.1f}".format(right_edge_dist) + " es" + "{:.1f}".format(self.lle_std_avg) +
                " fs" + "{:.1f}".format(self.rle_std_avg) + " s" + str(self.on_right_side))

    # check for infinite or lane change situation
    safe_idxs = np.isfinite(self.ll_t)
    if safe_idxs[0]:
      lane_path_y_interp = np.interp(path_t, self.ll_t[safe_idxs], final_path_y[safe_idxs])
      path_xyz[:, 1] = self.final_lane_plan_factor * lane_path_y_interp + (1.0 - self.final_lane_plan_factor) * path_xyz[:, 1]

    return path_xyz
