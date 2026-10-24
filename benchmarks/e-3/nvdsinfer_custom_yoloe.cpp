/**
 * DeepStream parser for the YOLOE-26s-seg (1, 41, 8400) detection tensor.
 *
 * Output layout: 4 (box cx,cy,w,h) + 5 (class scores for baked prompts) + 32 (prototype weights).
 * Only the detection head is used; segmentation masks are ignored for T1.
 *
 * Set AVA_DETECTION_JSONL to emit one benchmark-only JSON record per parser invocation.
 */

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <vector>

#include "nvdsinfer_custom_impl.h"

namespace
{

    constexpr int kNumClasses = 5;
    constexpr int kTotalChannels = 41;
    constexpr int kNumAnchors = 8400;
    constexpr float kNmsIouThreshold = 0.5F;
    constexpr std::size_t kKeepTopK = 200;

    std::atomic<unsigned long long> frameSequence{0};
    std::mutex outputMutex;

    float intersectionOverUnion(
        const NvDsInferObjectDetectionInfo &a,
        const NvDsInferObjectDetectionInfo &b)
    {
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
        const NvDsInferParseDetectionParams &params,
        unsigned int classId)
    {
        if (classId < params.perClassThreshold.size())
        {
            return params.perClassThreshold[classId];
        }
        return 0.0F;
    }

    struct LetterboxParams
    {
        float scale = 1.0F;
        float padX = 0.0F;
        float padY = 0.0F;
        bool valid = false;
    };

    LetterboxParams computeLetterbox(float modelW, float modelH)
    {
        const char *envW = std::getenv("AVA_SOURCE_WIDTH");
        const char *envH = std::getenv("AVA_SOURCE_HEIGHT");
        if (envW == nullptr || envH == nullptr)
        {
            return {};
        }
        const float srcW = std::atof(envW);
        const float srcH = std::atof(envH);
        if (srcW <= 0.0F || srcH <= 0.0F)
        {
            return {};
        }
        LetterboxParams p;
        p.scale = std::min(modelW / srcW, modelH / srcH);
        p.padX = (modelW - srcW * p.scale) / 2.0F;
        p.padY = (modelH - srcH * p.scale) / 2.0F;
        p.valid = true;
        return p;
    }

    void writeDetections(
        const std::vector<NvDsInferObjectDetectionInfo> &objects,
        const LetterboxParams &lb)
    {
        const char *outputPath = std::getenv("AVA_DETECTION_JSONL");
        if (outputPath == nullptr || outputPath[0] == '\0')
        {
            return;
        }

        const auto frame = frameSequence.fetch_add(1, std::memory_order_relaxed);
        std::lock_guard<std::mutex> guard(outputMutex);
        FILE *output = std::fopen(outputPath, "a");
        if (output == nullptr)
        {
            return;
        }

        std::fprintf(output, "{\"frame\":%llu,\"detections\":[", frame);
        for (std::size_t index = 0; index < objects.size(); ++index)
        {
            const auto &object = objects[index];
            float left = object.left;
            float top = object.top;
            float width = object.width;
            float height = object.height;
            if (lb.valid)
            {
                left = (left - lb.padX) / lb.scale;
                top = (top - lb.padY) / lb.scale;
                width = width / lb.scale;
                height = height / lb.scale;
            }
            std::fprintf(
                output,
                "%s{\"class_id\":%u,\"confidence\":%.6f,"
                "\"left\":%.3f,\"top\":%.3f,\"width\":%.3f,\"height\":%.3f}",
                index == 0 ? "" : ",",
                object.classId,
                object.detectionConfidence,
                left,
                top,
                width,
                height);
        }
        std::fputs("]}\n", output);
        std::fclose(output);
    }

} // namespace

extern "C" bool NvDsInferParseCustomYoloE(
    const std::vector<NvDsInferLayerInfo> &outputLayersInfo,
    const NvDsInferNetworkInfo &networkInfo,
    const NvDsInferParseDetectionParams &detectionParams,
    std::vector<NvDsInferObjectDetectionInfo> &objectList)
{
    if (outputLayersInfo.empty() || outputLayersInfo[0].buffer == nullptr)
    {
        return false;
    }

    const auto *blob = static_cast<const float *>(outputLayersInfo[0].buffer);
    const float imageWidth = static_cast<float>(networkInfo.width);
    const float imageHeight = static_cast<float>(networkInfo.height);
    std::vector<NvDsInferObjectDetectionInfo> selectedObjects;

    const unsigned int configuredClasses = detectionParams.numClassesConfigured;
    const unsigned int classCount =
        configuredClasses == 0
            ? kNumClasses
            : std::min(configuredClasses, static_cast<unsigned int>(kNumClasses));

    for (unsigned int classId = 0; classId < classCount; ++classId)
    {
        std::vector<NvDsInferObjectDetectionInfo> candidates;
        const float threshold = thresholdForClass(detectionParams, classId);

        for (int anchor = 0; anchor < kNumAnchors; ++anchor)
        {
            const float confidence =
                blob[(4 + classId) * kNumAnchors + anchor];
            if (confidence < threshold)
            {
                continue;
            }

            const float centerX = blob[anchor];
            const float centerY = blob[kNumAnchors + anchor];
            const float width = blob[2 * kNumAnchors + anchor];
            const float height = blob[3 * kNumAnchors + anchor];

            if (width <= 0.0F || height <= 0.0F)
            {
                continue;
            }

            NvDsInferObjectDetectionInfo det;
            std::memset(&det, 0, sizeof(det));
            det.left = centerX - width / 2.0F;
            det.top = centerY - height / 2.0F;
            det.width = width;
            det.height = height;
            det.classId = classId;
            det.detectionConfidence = confidence;

            // Clamp to image bounds
            det.left = std::max(0.0F, std::min(det.left, imageWidth));
            det.top = std::max(0.0F, std::min(det.top, imageHeight));
            det.width = std::min(det.width, imageWidth - det.left);
            det.height = std::min(det.height, imageHeight - det.top);

            candidates.push_back(det);
        }

        // NMS per class
        std::sort(candidates.begin(), candidates.end(),
                  [](const NvDsInferObjectDetectionInfo &a,
                     const NvDsInferObjectDetectionInfo &b)
                  { return a.detectionConfidence > b.detectionConfidence; });

        for (const auto &candidate : candidates)
        {
            bool suppressed = false;
            for (const auto &kept : selectedObjects)
            {
                if (kept.classId != candidate.classId)
                {
                    continue;
                }
                if (intersectionOverUnion(kept, candidate) > kNmsIouThreshold)
                {
                    suppressed = true;
                    break;
                }
            }
            if (!suppressed)
            {
                selectedObjects.push_back(candidate);
            }
        }
    }

    // Keep top-K by confidence
    if (selectedObjects.size() > kKeepTopK)
    {
        std::sort(selectedObjects.begin(), selectedObjects.end(),
                  [](const NvDsInferObjectDetectionInfo &a,
                     const NvDsInferObjectDetectionInfo &b)
                  { return a.detectionConfidence > b.detectionConfidence; });
        selectedObjects.resize(kKeepTopK);
    }

    objectList = std::move(selectedObjects);

    // Write JSONL in frame space (benchmark only)
    const LetterboxParams lb = computeLetterbox(imageWidth, imageHeight);
    writeDetections(objectList, lb);

    return true;
}
