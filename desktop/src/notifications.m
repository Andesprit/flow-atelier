// Apple's desktop delegate identifies the notification that was clicked.
#import <Foundation/Foundation.h>
#import <UserNotifications/UserNotifications.h>

extern bool window_visible(void);
extern void quit_status(int status);
extern void open_run(const char *flowID);

static NSDictionary *finishedRuns(NSDictionary *previous, NSArray *rows,
                                  void (^deliver)(NSDictionary *)) {
    NSMutableDictionary *current = [NSMutableDictionary dictionary];
    for (NSDictionary *row in rows) {
        NSString *identifier = row[@"flow_id"], *finished = row[@"finished_at"];
        if (![identifier isKindOfClass:NSString.class] || ![finished isKindOfClass:NSString.class])
            continue;
        current[identifier] = finished;
        if (previous && ![finished isEqual:previous[identifier]]) deliver(row);
    }
    return current;
}

static bool hasActiveRuns(NSArray *rows) {
    for (NSDictionary *row in rows) {
        if ([row[@"status"] isEqual:@"running"] || [row[@"status"] isEqual:@"needs_input"])
            return true;
    }
    return false;
}

static int pollDelay(bool visible, NSArray *rows) {
    return visible || hasActiveRuns(rows) ? 5 : 60;
}

@interface RunNotices : NSObject <UNUserNotificationCenterDelegate>
@property(nonatomic, strong) NSURLRequest *request;
@property(nonatomic, strong) NSDictionary *previous;
@end

static RunNotices *notices;

@implementation RunNotices
- (void)fetchRows:(void (^)(NSArray *))completion {
    [[[NSURLSession sharedSession] dataTaskWithRequest:self.request completionHandler:
      ^(NSData *data, NSURLResponse *response, NSError *error) {
        id rows = nil;
        if (((NSHTTPURLResponse *)response).statusCode == 200 && data)
            rows = [NSJSONSerialization JSONObjectWithData:data options:0 error:NULL];
        dispatch_async(dispatch_get_main_queue(), ^{
            completion([rows isKindOfClass:NSArray.class] ? rows : nil);
        });
    }] resume];
}

- (void)poll {
    [self fetchRows:^(NSArray *rows) {
        if (rows) {
            self.previous = finishedRuns(self.previous, rows, ^(NSDictionary *row) {
                UNMutableNotificationContent *content = [UNMutableNotificationContent new];
                content.title = [NSString stringWithFormat:@"Run %@", row[@"status"]];
                content.body = row[@"conduit_name"];
                UNNotificationRequest *notice = [UNNotificationRequest
                    requestWithIdentifier:row[@"flow_id"] content:content trigger:nil];
                [UNUserNotificationCenter.currentNotificationCenter addNotificationRequest:notice
                    withCompletionHandler:^(NSError *failure) {
                        if (failure) NSLog(@"Atelier notification failed: %@", failure.localizedDescription);
                    }];
            });
        }
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, pollDelay(window_visible(), rows) * NSEC_PER_SEC),
                       dispatch_get_main_queue(), ^{ [self poll]; });
    }];
}

- (void)userNotificationCenter:(UNUserNotificationCenter *)center
       willPresentNotification:(UNNotification *)notice
         withCompletionHandler:(void (^)(UNNotificationPresentationOptions))completion {
    completion(UNNotificationPresentationOptionBanner | UNNotificationPresentationOptionList);
}

- (void)userNotificationCenter:(UNUserNotificationCenter *)center
 didReceiveNotificationResponse:(UNNotificationResponse *)response
         withCompletionHandler:(void (^)(void))completion {
    if ([response.actionIdentifier isEqual:UNNotificationDefaultActionIdentifier])
        open_run(response.notification.request.identifier.UTF8String);
    completion();
}
@end

void check_quit(void) {
    [notices fetchRows:^(NSArray *rows) {
        quit_status(rows ? (int)hasActiveRuns(rows) : -1);
    }];
}

void start_notices(unsigned short port, const char *apiToken) {
    notices = [RunNotices new];
    NSURL *url = [NSURL URLWithString:[NSString stringWithFormat:@"http://127.0.0.1:%hu/flows", port]];
    NSMutableURLRequest *request = [NSMutableURLRequest requestWithURL:url];
    request.cachePolicy = NSURLRequestReloadIgnoringLocalCacheData;
    request.timeoutInterval = 5;
    NSString *token = [NSString stringWithUTF8String:apiToken];
    [request setValue:[@"Bearer " stringByAppendingString:token] forHTTPHeaderField:@"Authorization"];
    notices.request = request;
    UNUserNotificationCenter *center = UNUserNotificationCenter.currentNotificationCenter;
    center.delegate = notices;
    [center requestAuthorizationWithOptions:UNAuthorizationOptionAlert completionHandler:
        ^(BOOL granted, NSError *error) {
            if (error) NSLog(@"Atelier notification authorization failed: %@", error.localizedDescription);
            if (granted) [notices poll];
        }];
}
