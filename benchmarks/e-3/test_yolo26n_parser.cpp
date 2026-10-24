#include <cassert>
#include <fstream>
#include <iterator>
#include <string>

#include "nvdsinfer_custom_yolo26n.cpp"

namespace {

struct Fixture {
    std::vector<float> tensor = std::vector<float>(84 * 8400, 0.0F);
    NvDsInferNetworkInfo network{640, 640, 3};
    NvDsInferParseDetectionParams params{};
    std::vector<NvDsInferLayerInfo> layers{NvDsInferLayerInfo{}};
    std::vector<NvDsInferObjectDetectionInfo> objects;

    Fixture() {
        params.numClassesConfigured = 80;
        params.perClassThreshold.assign(80, 0.35F);
        layers[0].buffer = tensor.data();
    }

    void box(int anchor, int classId, float confidence,
             float centerX = 100.0F, float centerY = 100.0F,
             float width = 20.0F, float height = 20.0F) {
        tensor[anchor] = centerX;
        tensor[8400 + anchor] = centerY;
        tensor[2 * 8400 + anchor] = width;
        tensor[3 * 8400 + anchor] = height;
        tensor[(4 + classId) * 8400 + anchor] = confidence;
    }

    bool parse() {
        return NvDsInferParseCustomYolo26N(layers, network, params, objects);
    }
};

void test_missing_output_is_rejected() {
    Fixture missing;
    missing.layers.clear();
    assert(!missing.parse());
    assert(missing.objects.empty());

    Fixture nullBuffer;
    nullBuffer.layers[0].buffer = nullptr;
    assert(!nullBuffer.parse());
    assert(nullBuffer.objects.empty());
}

void test_threshold_and_model_coordinate_mapping() {
    Fixture fixture;
    fixture.box(0, 0, 0.8F);
    fixture.box(1, 1, 0.34F);
    assert(fixture.parse());
    assert(fixture.objects.size() == 1);
    const auto& object = fixture.objects[0];
    assert(object.classId == 0);
    assert(object.detectionConfidence == 0.8F);
    assert(object.left == 90.0F && object.top == 90.0F);
    assert(object.width == 20.0F && object.height == 20.0F);
}

void test_threshold_is_inclusive_and_class_specific() {
    Fixture fixture;
    fixture.params.perClassThreshold[1] = 0.7F;
    fixture.box(0, 0, 0.35F);
    fixture.box(1, 1, 0.6F);
    assert(fixture.parse());
    assert(fixture.objects.size() == 1);
    assert(fixture.objects[0].classId == 0);
}

void test_nms_suppresses_only_overlapping_same_class_boxes() {
    Fixture fixture;
    fixture.box(0, 0, 0.9F);
    fixture.box(1, 0, 0.8F);
    fixture.box(2, 1, 0.7F);
    fixture.box(3, 0, 0.6F, 200.0F, 200.0F);
    assert(fixture.parse());
    assert(fixture.objects.size() == 3);
    assert(fixture.objects[0].detectionConfidence == 0.9F);
    assert(fixture.objects[1].classId == 1);
    assert(fixture.objects[2].left == 190.0F);
}

void test_configured_class_limit_is_respected() {
    Fixture fixture;
    fixture.params.numClassesConfigured = 1;
    fixture.box(0, 0, 0.8F);
    fixture.box(1, 1, 0.9F);
    assert(fixture.parse());
    assert(fixture.objects.size() == 1);
    assert(fixture.objects[0].classId == 0);
}

void test_right_and_bottom_edges_are_clipped_and_zero_area_is_discarded() {
    Fixture fixture;
    fixture.box(0, 0, 0.9F, 639.0F, 639.0F, 10.0F, 10.0F);
    fixture.box(1, 0, 0.8F, 100.0F, 100.0F, 0.0F, 20.0F);
    fixture.box(2, 0, 0.7F, 700.0F, 700.0F);
    assert(fixture.parse());
    assert(fixture.objects.size() == 1);
    assert(fixture.objects[0].left == 634.0F);
    assert(fixture.objects[0].top == 634.0F);
    assert(fixture.objects[0].width == 6.0F);
    assert(fixture.objects[0].height == 6.0F);
}

void test_global_topk_keeps_highest_scores_across_classes() {
    Fixture fixture;
    for (int anchor = 0; anchor < 220; ++anchor) {
        fixture.box(anchor, 0, 0.5F,
                    10.0F + (anchor % 22) * 28.0F,
                    10.0F + (anchor / 22) * 28.0F, 5.0F, 5.0F);
    }
    fixture.box(220, 1, 0.95F);
    assert(fixture.parse());
    assert(fixture.objects.size() == 200);
    assert(fixture.objects[0].classId == 1);
    assert(fixture.objects[0].detectionConfidence == 0.95F);
}

void test_jsonl_includes_empty_frames_and_appends(const char* outputPath) {
    std::ofstream(outputPath, std::ios::trunc).close();
    assert(setenv("AVA_DETECTION_JSONL", outputPath, 1) == 0);
    Fixture empty;
    assert(empty.parse());
    Fixture detected;
    detected.box(0, 0, 0.9F);
    assert(detected.parse());
    unsetenv("AVA_DETECTION_JSONL");

    std::ifstream output(outputPath);
    const std::string content{
        std::istreambuf_iterator<char>(output), std::istreambuf_iterator<char>()};
    assert(content.find("{\"frame\":0,\"detections\":[]}\n") == 0);
    assert(content.find("{\"frame\":1,\"detections\":[{\"class_id\":0,") != std::string::npos);
    assert(std::count(content.begin(), content.end(), '\n') == 2);
}

}  // namespace

int main(int argc, char** argv) {
    assert(argc == 2);
    unsetenv("AVA_DETECTION_JSONL");
    test_missing_output_is_rejected();
    test_threshold_and_model_coordinate_mapping();
    test_threshold_is_inclusive_and_class_specific();
    test_nms_suppresses_only_overlapping_same_class_boxes();
    test_configured_class_limit_is_respected();
    test_right_and_bottom_edges_are_clipped_and_zero_area_is_discarded();
    test_global_topk_keeps_highest_scores_across_classes();
    test_jsonl_includes_empty_frames_and_appends(argv[1]);
    std::puts("PASS: 8 parser contract tests");
}
