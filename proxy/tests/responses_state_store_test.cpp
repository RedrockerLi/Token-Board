#include "responses_state_store.h"

#include <cassert>
#include <string>
#include <vector>

using json = nlohmann::json;

int main() {
    ResponsesStateStore store;
    assert(store.record("r1", {json{{"type", "message"}, {"text", "in"}}},
                        {json{{"type", "message"}, {"text", "out"}}}));
    std::vector<json> items;
    assert(store.lookup("r1", items));
    assert(items.size() == 2 && items[0]["text"] == "in" && items[1]["text"] == "out");
    for (int i = 2; i <= 513; ++i)
        assert(store.record("r" + std::to_string(i), {json{{"i", i}}}, {}));
    assert(store.size() == ResponsesStateStore::kMaxResponses);
    assert(!store.lookup("r1", items));
    assert(store.lookup("r513", items));
    const std::string large(ResponsesStateStore::kMaxBytes, 'x');
    assert(!store.record("too-large", {json{{"data", large}}}, {}));
    return 0;
}
