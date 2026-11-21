// Apple's desktop delegate identifies the notification that was clicked.
#import <Foundation/Foundation.h>
#import <UserNotifications/UserNotifications.h>

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

@interface RunNotices : NSObject <UNUserNotificationCenterDelegate>
@property(nonatomic, strong) NSURLRequest *request;
@property(nonatomic, strong) NSDictionary *previous;
@end

@implementation RunNotices
- (void)poll {
    [[[NSURLSession sharedSession] dataTaskWithRequest:self.request completionHandler:
      ^(NSData *data, NSURLResponse *response, NSError *error) {
        if (((NSHTTPURLResponse *)response).statusCode == 200 && data) {
            NSArray *rows = [NSJSONSerialization JSONObjectWithData:data options:0 error:NULL];
            if ([rows isKindOfClass:NSArray.class]) {
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
        }
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 5 * NSEC_PER_SEC),
                       dispatch_get_main_queue(), ^{ [self poll]; });
    }] resume];
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

void start_notices(unsigned short port) {
    static RunNotices *notices;
    notices = [RunNotices new];
    NSURL *url = [NSURL URLWithString:[NSString stringWithFormat:@"http://127.0.0.1:%hu/flows", port]];
    NSMutableURLRequest *request = [NSMutableURLRequest requestWithURL:url];
    request.cachePolicy = NSURLRequestReloadIgnoringLocalCacheData;
    NSString *token = NSProcessInfo.processInfo.environment[@"ATELIER_API_TOKEN"];
    if (token.length)
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
