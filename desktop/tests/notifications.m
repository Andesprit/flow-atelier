#import "../src/notifications.m"
#include <assert.h>

void open_run(const char *id) {}
bool window_visible(void) { return false; }
void quit_status(int status) {}
static bool allowQuit;
static bool decision, appKitDecision;
void quit_decided(bool quit, bool appKit) { decision = quit; appKitDecision = appKit; }
@interface TestApplication : NSObject
@property(nonatomic) NSUInteger replies;
@property(nonatomic) BOOL answer;
@end
@implementation TestApplication
- (void)replyToApplicationShouldTerminate:(BOOL)answer {
    self.replies++;
    self.answer = answer;
}
@end
bool confirm_quit(void) { return allowQuit; }
@interface TestAppDelegate : NSObject
@end
@implementation TestAppDelegate
@end

int main(void) {
    @autoreleasepool {
        TestAppDelegate *delegate = [TestAppDelegate new];
        assert(installQuitHandler(delegate));
        // Cocoa calls this selector directly for Command-Q and Dock Quit.
        id<NSApplicationDelegate> appDelegate = (id)delegate;
        TestApplication *app = [TestApplication new];
        assert([appDelegate applicationShouldTerminate:(id)app] == NSTerminateLater);
        assert(app.replies == 0);
        finish_quit(false);
        assert(app.replies == 1 && !app.answer && !decision && appKitDecision);
        assert([appDelegate applicationShouldTerminate:(id)app] == NSTerminateLater);
        finish_quit(true);
        assert(app.replies == 2 && app.answer && decision && appKitDecision);
        // A tray-only quit does not reply to a previous AppKit request.
        finish_quit(true);
        assert(app.replies == 2 && decision && !appKitDecision);
        allowQuit = true;
        assert([appDelegate applicationShouldTerminate:NSApp] == NSTerminateNow);
        NSDictionary *first = @{@"flow_id": @"a", @"finished_at": @"earlier", @"status": @"completed"};
        NSDictionary *second = @{@"flow_id": @"b", @"finished_at": @"now", @"status": @"failed"};
        NSDictionary *active = @{@"flow_id": @"c", @"finished_at": NSNull.null, @"status": @"running"};
        assert(pollDelay(false, @[active]) == 5);
        assert(pollDelay(true, @[first]) == 5);
        assert(pollDelay(false, @[first]) > 5);
        assert(hasActiveRuns(@[@{@"status": @"needs_input"}]));
        assert(!hasActiveRuns(@[first, second]));
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
