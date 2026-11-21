#import "../src/notifications.m"
#include <assert.h>

void open_run(const char *id) {}

int main(void) {
    @autoreleasepool {
        NSDictionary *first = @{@"flow_id": @"a", @"finished_at": @"earlier", @"status": @"completed"};
        NSDictionary *second = @{@"flow_id": @"b", @"finished_at": @"now", @"status": @"failed"};
        NSDictionary *active = @{@"flow_id": @"c", @"finished_at": NSNull.null, @"status": @"running"};
        NSMutableArray *notices = [NSMutableArray array];
        void (^send)(NSDictionary *) = ^(NSDictionary *row){ [notices addObject:row[@"flow_id"]]; };
        NSDictionary *previous = finishedRuns(nil, @[first, active], send);
        assert(notices.count == 0);
        previous = finishedRuns(previous, @[first, second, active], send);
        previous = finishedRuns(previous, @[first, second, active], send);
        assert([notices isEqual:@[@"b"]]);
        NSDictionary *last = @{@"flow_id": @"c", @"finished_at": @"later", @"status": @"completed"};
        finishedRuns(previous, @[first, second, last], send);
        assert(([notices isEqual:@[@"b", @"c"]]));
    }
}
