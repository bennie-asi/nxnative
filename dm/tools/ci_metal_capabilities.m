// Record runtime GPU support separately from the offline Metal compiler.
// A hosted VM may compile iPhone libraries while lacking Metal 4 resources.
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <stdio.h>

int main(void) {
    @autoreleasepool {
        id<MTLDevice> device = MTLCreateSystemDefaultDevice();
        NSMutableDictionary *result = [@{@"device": device.name ?: @"none", @"has_device": @(device != nil)} mutableCopy];
        if (device) {
            result[@"unified_memory"] = @(device.hasUnifiedMemory);
            result[@"apple7"] = @([device supportsFamily:MTLGPUFamilyApple7]);
            result[@"apple9"] = @([device supportsFamily:MTLGPUFamilyApple9]);
            NSError *error = nil;
            MTLResidencySetDescriptor *desc = [MTLResidencySetDescriptor new]; desc.initialCapacity = 4096;
            id<MTLResidencySet> residency = [device newResidencySetWithDescriptor:desc error:&error];
            result[@"residency_set"] = @(residency != nil);
            if (error) result[@"residency_error"] = error.localizedDescription;
            error = nil;
            id<MTL4CommandQueue> queue = [device newMTL4CommandQueueWithDescriptor:[MTL4CommandQueueDescriptor new] error:&error];
            result[@"metal4_queue"] = @(queue != nil);
            if (error) result[@"metal4_queue_error"] = error.localizedDescription;
        }
        result[@"purpose"] = @"Runtime capabilities only; this does not test a game or an iPhone.";
        NSData *json = [NSJSONSerialization dataWithJSONObject:result options:NSJSONWritingPrettyPrinted error:nil];
        puts([[NSString alloc] initWithData:json encoding:NSUTF8StringEncoding].UTF8String);
    }
    return 0;
}
