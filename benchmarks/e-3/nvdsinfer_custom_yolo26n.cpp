/**
 * DeepStream parser for the YOLO26n (1, 84, 8400) detection tensor.
 *
 * Set AVA_DETECTION_JSONL to emit one benchmark-only JSON record per parser
 * invocation. Production metadata continues through DeepStream's object list.
 */

#include <algorithm>
#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <mutex>
#include <vector>

#include "nvdsinfer_custom_impl.h"

namespace {

constexpr int kNumClasses = 80;
constexpr int kNumAnchors = 8400;
constexpr float kNmsIouThreshold = 0.5F;
constexpr std::size_t kKeepTopK = 200;

std::atomic<unsigned long long> frameSequence{0};
std::mutex outputMutex;

float intersectionOverUnion(
    const NvDsInferObjectDetectionInfo& a,
    const NvDsInferObjectDetectionInfo& b) {
    const float interLeft = std::max(a.left, b.left);
    const float interTop = std::max(a.top, b.top);
    const float interRight = std::min(a.left + a.width, b.left + b.width);
    const float interBottom = std::min(a.top + a.height, b.top + b.height);
    const float interArea =
        std::max(0.0F, interRight - interLeft) *
        std::max(0.0F, interBottom - interTop);
    const float unionArea =
        a.width * a.height + b.width * b.height - interArea;
    return unionArea > 0.0F ? interArea / unionArea : 0.0F;
}

float thresholdForClass(
    const NvDsInferParseDetectionParams& params,
    unsigned int classId) {
    if (classId < params.perClassThreshold.size()) {
        return params.perClassThreshold[classId];
    }
    return 0.0F;
}

void writeDetections(const std::vector<NvDsInferObjectDetectionInfo>& objects) {
    const char* outputPath = std::getenv("AVA_DETECTION_JSONL");
    if (outputPath == nullptr || outputPath[0] == '\0') {
        return;
    }

    const auto frame = frameSequence.fetch_add(1, std::memory_order_relaxed);
    std::lock_guard<std::mutex> guard(outputMutex);
    FILE* output = std::fopen(outputPath, "a");
    if (output == nullptr) {
        return;
    }

    std::fprintf(output, "{\"frame\":%llu,\"detections\":[", frame);
    for (std::size_t index = 0; index < objects.size(); ++index) {
        const auto& object = objects[index];
        std::fprintf(
            output,
            "%s{\"class_id\":%u,\"confidence\":%.6f,"
            "\"left\":%.3f,\"top\":%.3f,\"width\":%.3f,\"height\":%.3f}",
            index == 0 ? "" : ",",
            object.classId,
            object.detectionConfidence,
            object.left,
            object.top,
            object.width,
            object.height);
    }
    std::fputs("]}\n", output);
    std::fclose(output);
}

}  // namespace

extern "C" bool NvDsInferParseCustomYolo26N(
    const std::vector<NvDsInferLayerInfo>& outputLayersInfo,
    const NvDsInferNetworkInfo& networkInfo,
    const NvDsInferParseDetectionParams& detectionParams,
    std::vector<NvDsInferObjectDetectionInfo>& objectList) {
    if (outputLayersInfo.empty() || outputLayersInfo[0].buffer == nullptr) {
        return false;
    }

    const auto* blob = static_cast<const float*>(outputLayersInfo[0].buffer);
    const float imageWidth = static_cast<float>(networkInfo.width);
    const float imageHeight = static_cast<float>(networkInfo.height);
    std::vector<NvDsInferObjectDetectionInfo> selectedObjects;

    const unsigned int configuredClasses = detectionParams.numClassesConfigured;
    const unsigned int classCount =
        configuredClasses == 0
            ? kNumClasses
            : std::min(configuredClasses, static_cast<unsigned int>(kNumClasses));

    for (unsigned int classId = 0; classId < classCount; ++classId) {
        std::vector<NvDsInferObjectDetectionInfo> candidates;
        const float threshold = thresholdForClass(detectionParams, classId);

        for (int anchor = 0; anchor < kNumAnchors; ++anchor) {
            const float confidence =
                blob[(4 + classId) * kNumAnchors + anchor];
            if (confidence < threshold) {
                continue;
            }

            const float centerX = blob[anchor];
            const float centerY = blob[kNumAnchors + anchor];
            float width = blob[2 * kNumAnchors + anchor];
            float height = blob[3 * kNumAnchors + anchor];
            float left = centerX - width / 2.0F;
            float top = centerY - height / 2.0F;

            left = std::clamp(left, 0.0F, imageWidth);
            top = std::clamp(top, 0.0F, imageHeight);
            width = std::min(width, imageWidth - left);
            height = std::min(height, imageHeight - top);
            if (width <= 0.0F || height <= 0.0F) {
                continue;
            }

            NvDsInferObjectDetectionInfo object{};
            object.classId = classId;
            object.left = left;
            object.top = top;
            object.width = width;
            object.height = height;
            object.detectionConfidence = confidence;
            candidates.push_back(object);
        }

        std::sort(
            candidates.begin(),
            candidates.end(),
            [](const auto& left, const auto& right) {
                return left.detectionConfidence > right.detectionConfidence;
            });

        for (const auto& candidate : candidates) {
            const bool suppressed = std::any_of(
                selectedObjects.begin(),
                selectedObjects.end(),
                [&candidate](const auto& selected) {
                    return selected.classId == candidate.classId &&
                        intersectionOverUnion(selected, candidate) >
                            kNmsIouThreshold;
                });
            if (!suppressed) {
                selectedObjects.push_back(candidate);
            }
        }
    }

    std::sort(
        selectedObjects.begin(),
        selectedObjects.end(),
        [](const auto& left, const auto& right) {
            return left.detectionConfidence > right.detectionConfidence;
        });
    if (selectedObjects.size() > kKeepTopK) {
        selectedObjects.resize(kKeepTopK);
    }

    objectList.insert(
        objectList.end(), selectedObjects.begin(), selectedObjects.end());
    writeDetections(selectedObjects);
    return true;
}

CHECK_CUSTOM_PARSE_FUNC_PROTOTYPE(NvDsInferParseCustomYolo26N);
